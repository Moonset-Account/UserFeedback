#pragma once

// Depth-limited, non-recursive event evaluator for pre-parsed policy
// configuration documents.
//
// The native policy service pre-parses each configuration document into a
// flat stream of events (container enter, field read, container exit).
// Historically these events were consumed by a recursive descent evaluator,
// which could exhaust the call stack on deeply nested hostile input
// (SEC-VULN-017). This evaluator is iterative and enforces a configurable
// per-document depth budget instead:
//
//   * Depth is counted independently for every document.
//   * Entering a container that would exceed the budget fails the document
//     BEFORE the container is entered; the error carries the 0-based event
//     number within that document's own event stream.
//   * Input may be delivered in batches; processing can be paused and
//     resumed across batches (an enter and its matching exit may live in
//     different batches).
//   * A failed or cancelled document publishes nothing and leaves no state
//     behind for the next document.

#include <cstddef>
#include <string>
#include <utility>
#include <vector>

namespace policy {

enum class EventType { EnterContainer, FieldRead, ExitContainer };

struct Event {
  EventType type;
  std::string name;   // container name (EnterContainer) or field key (FieldRead)
  std::string value;  // field value (FieldRead only)

  static Event enter(std::string container_name) {
    return Event{EventType::EnterContainer, std::move(container_name), {}};
  }
  static Event field(std::string key, std::string val) {
    return Event{EventType::FieldRead, std::move(key), std::move(val)};
  }
  static Event exit() { return Event{EventType::ExitContainer, {}, {}}; }
};

// A recorded evaluation failure. Part of the durable result records.
struct EvalError {
  std::string doc_id;
  std::size_t event_no;  // 0-based index within the document's own event stream
  std::string message;
};

struct FieldRecord {
  std::string path;   // "/container/.../key"
  std::string value;
};

// A fully and successfully evaluated document. Only ever produced by a
// successful end_document(); partial policies are never published.
struct PublishedPolicy {
  std::string doc_id;
  std::vector<FieldRecord> fields;
};

enum class FeedStatus {
  Ok,          // batch fully consumed
  Paused,      // evaluator is paused; batch NOT consumed
  NoDocument,  // no active document; batch NOT consumed
  Failed       // document already failed (or failed inside this batch)
};

class DepthLimitedEvaluator {
 public:
  explicit DepthLimitedEvaluator(std::size_t max_depth);

  // Opens a new document with a fresh depth counter and event numbering.
  // Any previously active document must have been ended or cancelled first.
  // Returns false if a document is already active.
  bool begin_document(std::string doc_id);

  // Consumes one batch of events for the active document. State persists
  // between calls, so callers may pause between batches and resume later.
  // Returns:
  //   Ok         - every event in the batch was consumed;
  //   Paused     - evaluator paused, nothing consumed (call resume());
  //   NoDocument - no active document, nothing consumed;
  //   Failed     - the document failed (inside this batch or earlier);
  //                events after the failing one were not consumed.
  FeedStatus feed(const std::vector<Event>& batch);

  // Finalizes the active document. On success the assembled policy is
  // appended to published(). On failure (document already failed, or
  // containers left unclosed) nothing is published. Either way the
  // per-document state is discarded. Returns true iff a policy was
  // published.
  bool end_document();

  // Discards the active document without publishing anything.
  void cancel();

  // Explicit pause/resume. While paused, feed() returns Paused and consumes
  // nothing; resume() continues exactly where processing stopped.
  void pause();
  void resume();

  bool has_active_document() const;
  bool paused() const;
  bool current_failed() const;
  std::size_t current_depth() const;         // open containers of active doc
  std::size_t current_event_index() const;   // events consumed by active doc
  std::size_t max_depth() const;

  // Durable result records, kept across documents.
  const std::vector<PublishedPolicy>& published() const { return published_; }
  const std::vector<EvalError>& errors() const { return errors_; }

 private:
  void fail_active(const std::string& message);
  void reset_active();

  std::size_t max_depth_;

  // Per-document state (all reset for every new document).
  bool doc_active_ = false;
  bool doc_failed_ = false;
  std::string doc_id_;
  std::size_t depth_ = 0;
  std::size_t event_index_ = 0;
  std::vector<std::string> container_stack_;
  std::vector<FieldRecord> pending_fields_;

  bool paused_ = false;

  // Cross-document result records.
  std::vector<PublishedPolicy> published_;
  std::vector<EvalError> errors_;
};

}  // namespace policy
