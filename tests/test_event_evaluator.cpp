// Native C++17 tests for the depth-limited event evaluator (SEC-VULN-017).
//
// Each requirement from the task maps to a stable case_id:
//   T1_max_legal_depth            - nesting exactly at the budget succeeds
//   T2_exceed_by_one_level        - one level over fails BEFORE the enter,
//                                   error carries the document-local event no
//   T3_enter_exit_split_batches   - enter/exit in different batches, pause
//                                   and resume between batches
//   T4_cancel_then_new_document   - cancel discards state; next document
//                                   counts depth independently
//   T5_result_records_isolation   - over-limit document publishes nothing
//                                   and does not pollute the next document
//   T6_unbalanced_exit_rejected   - exit at root fails with event number
//   T7_unclosed_containers_fail   - end with open containers publishes nothing
//
// The process exits non-zero if any check fails or no tests ran.

#include <cstdio>
#include <string>
#include <vector>

#include "../src/event_evaluator.h"

namespace {

int g_checks = 0;
int g_failures = 0;
int g_cases = 0;

#define CHECK(cond)                                              \
  do {                                                           \
    ++g_checks;                                                  \
    if (!(cond)) {                                               \
      ++g_failures;                                              \
      std::printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
    }                                                            \
  } while (0)

void begin_case(const char* case_id) {
  ++g_cases;
  std::printf("CASE %s\n", case_id);
}

using policy::DepthLimitedEvaluator;
using policy::Event;
using policy::FeedStatus;

// T1: nesting exactly at the maximum depth is legal and publishes.
void t1_max_legal_depth() {
  begin_case("T1_max_legal_depth");
  DepthLimitedEvaluator ev(3);
  CHECK(ev.begin_document("doc-legal"));
  CHECK(ev.feed({Event::enter("a"), Event::enter("b"), Event::enter("c"),
                 Event::field("mode", "enforce"), Event::exit(), Event::exit(),
                 Event::exit()}) == FeedStatus::Ok);
  CHECK(ev.current_depth() == 0);
  CHECK(ev.current_event_index() == 7);
  CHECK(ev.end_document());
  CHECK(ev.published().size() == 1);
  CHECK(ev.errors().empty());
  if (ev.published().size() == 1) {
    CHECK(ev.published()[0].doc_id == "doc-legal");
    CHECK(ev.published()[0].fields.size() == 1);
    if (ev.published()[0].fields.size() == 1) {
      CHECK(ev.published()[0].fields[0].path == "/a/b/c/mode");
      CHECK(ev.published()[0].fields[0].value == "enforce");
    }
  }
}

// T2: one level over the budget fails before the container is entered, and
// the error carries the document-local event number.
void t2_exceed_by_one_level() {
  begin_case("T2_exceed_by_one_level");
  DepthLimitedEvaluator ev(3);
  CHECK(ev.begin_document("doc-too-deep"));
  // Events 0..2 enter a,b,c (depth 3, legal). Event 3 would reach depth 4.
  CHECK(ev.feed({Event::enter("a"), Event::enter("b"), Event::enter("c"),
                 Event::enter("d")}) == FeedStatus::Failed);
  // The rejected enter mutated nothing: depth and consumed count stop at 3.
  CHECK(ev.current_depth() == 3);
  CHECK(ev.current_event_index() == 3);
  CHECK(ev.current_failed());
  CHECK(ev.errors().size() == 1);
  if (ev.errors().size() == 1) {
    CHECK(ev.errors()[0].doc_id == "doc-too-deep");
    CHECK(ev.errors()[0].event_no == 3);  // the rejected EnterContainer
    CHECK(ev.errors()[0].message.find("depth limit exceeded") !=
          std::string::npos);
  }
  // A failed document rejects further input without consuming it.
  CHECK(ev.feed({Event::exit()}) == FeedStatus::Failed);
  CHECK(ev.current_event_index() == 3);
  // No partial policy is published.
  CHECK(!ev.end_document());
  CHECK(ev.published().empty());
  CHECK(!ev.has_active_document());
}

// T3: enter and its matching exit arrive in different batches; processing
// pauses between batches and resumes with state intact.
void t3_enter_exit_split_batches() {
  begin_case("T3_enter_exit_split_batches");
  DepthLimitedEvaluator ev(4);
  CHECK(ev.begin_document("doc-batched"));
  CHECK(ev.feed({Event::enter("outer")}) == FeedStatus::Ok);
  CHECK(ev.current_depth() == 1);

  ev.pause();
  CHECK(ev.paused());
  // While paused nothing is consumed.
  CHECK(ev.feed({Event::enter("inner")}) == FeedStatus::Paused);
  CHECK(ev.current_depth() == 1);
  CHECK(ev.current_event_index() == 1);

  ev.resume();
  CHECK(!ev.paused());
  // The paused batch is now consumed, the exit events arrive in a third one.
  CHECK(ev.feed({Event::enter("inner"), Event::field("k", "v")}) ==
        FeedStatus::Ok);
  CHECK(ev.current_depth() == 2);
  CHECK(ev.feed({Event::exit(), Event::exit()}) == FeedStatus::Ok);
  CHECK(ev.current_depth() == 0);
  CHECK(ev.end_document());
  CHECK(ev.published().size() == 1);
  if (ev.published().size() == 1 && ev.published()[0].fields.size() == 1) {
    CHECK(ev.published()[0].fields[0].path == "/outer/inner/k");
  }
}

// T4: cancelling a document discards its state; the next document counts
// depth independently from zero.
void t4_cancel_then_new_document() {
  begin_case("T4_cancel_then_new_document");
  DepthLimitedEvaluator ev(2);
  CHECK(ev.begin_document("doc-cancelled"));
  CHECK(ev.feed({Event::enter("a"), Event::enter("b")}) == FeedStatus::Ok);
  CHECK(ev.current_depth() == 2);
  ev.cancel();
  CHECK(!ev.has_active_document());
  CHECK(ev.published().empty());

  // The new document starts with a fresh budget: two levels fit again.
  CHECK(ev.begin_document("doc-fresh"));
  CHECK(ev.current_depth() == 0);
  CHECK(ev.current_event_index() == 0);
  CHECK(ev.feed({Event::enter("x"), Event::enter("y"),
                 Event::field("k", "v"), Event::exit(), Event::exit()}) ==
        FeedStatus::Ok);
  CHECK(ev.end_document());
  CHECK(ev.published().size() == 1);
  CHECK(ev.errors().empty());
  if (ev.published().size() == 1) {
    CHECK(ev.published()[0].doc_id == "doc-fresh");
  }
}

// T5: result records prove the over-limit document published no partial
// policy and did not pollute the following document.
void t5_result_records_isolation() {
  begin_case("T5_result_records_isolation");
  DepthLimitedEvaluator ev(2);

  CHECK(ev.begin_document("doc-bad"));
  CHECK(ev.feed({Event::enter("a"), Event::field("leak", "no"),
                 Event::enter("b"), Event::enter("c")}) == FeedStatus::Failed);
  CHECK(!ev.end_document());

  CHECK(ev.begin_document("doc-good"));
  CHECK(ev.feed({Event::enter("p"), Event::field("mode", "enforce"),
                 Event::exit()}) == FeedStatus::Ok);
  CHECK(ev.end_document());

  // Exactly one policy, belonging to the good document, with only its field.
  CHECK(ev.published().size() == 1);
  if (ev.published().size() == 1) {
    CHECK(ev.published()[0].doc_id == "doc-good");
    CHECK(ev.published()[0].fields.size() == 1);
    if (ev.published()[0].fields.size() == 1) {
      CHECK(ev.published()[0].fields[0].path == "/p/mode");
    }
  }
  // The failure is on record with the failing document's id and event no.
  CHECK(ev.errors().size() == 1);
  if (ev.errors().size() == 1) {
    CHECK(ev.errors()[0].doc_id == "doc-bad");
    CHECK(ev.errors()[0].event_no == 3);
  }
}

// T6: an exit with no open container fails with the event number recorded.
void t6_unbalanced_exit_rejected() {
  begin_case("T6_unbalanced_exit_rejected");
  DepthLimitedEvaluator ev(4);
  CHECK(ev.begin_document("doc-unbalanced"));
  CHECK(ev.feed({Event::field("k", "v"), Event::exit()}) == FeedStatus::Failed);
  CHECK(ev.errors().size() == 1);
  if (ev.errors().size() == 1) {
    CHECK(ev.errors()[0].event_no == 1);
    CHECK(ev.errors()[0].message.find("unbalanced") != std::string::npos);
  }
  CHECK(!ev.end_document());
  CHECK(ev.published().empty());
}

// T7: ending a document with containers still open fails and publishes
// nothing.
void t7_unclosed_containers_fail() {
  begin_case("T7_unclosed_containers_fail");
  DepthLimitedEvaluator ev(4);
  CHECK(ev.begin_document("doc-unclosed"));
  CHECK(ev.feed({Event::enter("a"), Event::field("k", "v")}) ==
        FeedStatus::Ok);
  CHECK(!ev.end_document());
  CHECK(ev.published().empty());
  CHECK(ev.errors().size() == 1);
  if (ev.errors().size() == 1) {
    CHECK(ev.errors()[0].message.find("still open") != std::string::npos);
  }
  // The evaluator is ready for a clean next document.
  CHECK(ev.begin_document("doc-after"));
  CHECK(ev.feed({Event::field("ok", "yes")}) == FeedStatus::Ok);
  CHECK(ev.end_document());
  CHECK(ev.published().size() == 1);
}

}  // namespace

int main() {
  t1_max_legal_depth();
  t2_exceed_by_one_level();
  t3_enter_exit_split_batches();
  t4_cancel_then_new_document();
  t5_result_records_isolation();
  t6_unbalanced_exit_rejected();
  t7_unclosed_containers_fail();

  std::printf("TOTAL cases=%d checks=%d failures=%d\n", g_cases, g_checks,
              g_failures);
  if (g_cases == 0 || g_checks == 0) {
    std::printf("RESULT FAIL (no tests ran)\n");
    return 2;
  }
  if (g_failures != 0) {
    std::printf("RESULT FAIL\n");
    return 1;
  }
  std::printf("RESULT PASS\n");
  return 0;
}
