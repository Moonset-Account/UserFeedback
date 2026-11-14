// Command-line adapter for the depth-limited event evaluator.
//
// Reads a simple line-based event script from stdin and prints the durable
// result records (published policies and errors) to stdout.
//
// Input format (one command per line, tokens separated by whitespace):
//   maxdepth <n>        set the depth budget (before the first begin)
//   begin <doc_id>      open a new document
//   enter <name>        EnterContainer event
//   field <key> <value> FieldRead event
//   exit                ExitContainer event
//   pause / resume      pause / resume batch processing
//   end                 finalize the active document
//   cancel              discard the active document
//   # ...               comment line
//
// Output:
//   PUBLISH <doc_id> fields=<n>
//   FIELD <path> = <value>
//   ERROR <doc_id> event=<no> <message>
//   SUMMARY published=<n> errors=<n>
//
// Exit code is 0 when the script was processed (individual document
// failures are reported as ERROR records, not as process failure).

#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "event_evaluator.h"

namespace {

const char* status_name(policy::FeedStatus s) {
  switch (s) {
    case policy::FeedStatus::Ok:
      return "Ok";
    case policy::FeedStatus::Paused:
      return "Paused";
    case policy::FeedStatus::NoDocument:
      return "NoDocument";
    case policy::FeedStatus::Failed:
      return "Failed";
  }
  return "?";
}

}  // namespace

int main(int argc, char** argv) {
  std::size_t max_depth = 32;
  for (int i = 1; i + 1 < argc; ++i) {
    if (std::string(argv[i]) == "--max-depth") {
      max_depth = static_cast<std::size_t>(std::stoul(argv[i + 1]));
    }
  }

  policy::DepthLimitedEvaluator evaluator(max_depth);
  std::vector<policy::Event> pending_batch;
  bool started = false;  // maxdepth is only honored before the first begin

  auto flush = [&]() {
    if (pending_batch.empty()) {
      return;
    }
    policy::FeedStatus st = evaluator.feed(pending_batch);
    if (st != policy::FeedStatus::Ok) {
      std::cout << "FEED_STATUS " << status_name(st) << "\n";
    }
    pending_batch.clear();
  };

  std::string line;
  while (std::getline(std::cin, line)) {
    std::istringstream in(line);
    std::string cmd;
    if (!(in >> cmd) || cmd[0] == '#') {
      continue;
    }
    if (cmd == "maxdepth") {
      flush();
      std::size_t n;
      in >> n;
      if (started) {
        std::cout << "MAXDEPTH_REJECTED must precede the first begin\n";
      } else {
        max_depth = n;
        evaluator = policy::DepthLimitedEvaluator(max_depth);
      }
    } else if (cmd == "begin") {
      flush();
      started = true;
      std::string id;
      in >> id;
      if (!evaluator.begin_document(id)) {
        std::cout << "BEGIN_REJECTED " << id << "\n";
      }
    } else if (cmd == "enter") {
      std::string name;
      in >> name;
      pending_batch.push_back(policy::Event::enter(name));
    } else if (cmd == "field") {
      std::string key, value;
      in >> key >> value;
      pending_batch.push_back(policy::Event::field(key, value));
    } else if (cmd == "exit") {
      pending_batch.push_back(policy::Event::exit());
    } else if (cmd == "pause") {
      flush();
      evaluator.pause();
    } else if (cmd == "resume") {
      evaluator.resume();
    } else if (cmd == "end") {
      flush();
      bool ok = evaluator.end_document();
      std::cout << (ok ? "END_OK" : "END_FAILED") << "\n";
    } else if (cmd == "cancel") {
      flush();
      evaluator.cancel();
      std::cout << "CANCELLED\n";
    } else {
      std::cout << "UNKNOWN_COMMAND " << cmd << "\n";
    }
  }
  flush();

  // Emit the durable result records.
  for (const auto& p : evaluator.published()) {
    std::cout << "PUBLISH " << p.doc_id << " fields=" << p.fields.size()
              << "\n";
    for (const auto& f : p.fields) {
      std::cout << "FIELD " << f.path << " = " << f.value << "\n";
    }
  }
  for (const auto& e : evaluator.errors()) {
    std::cout << "ERROR " << e.doc_id << " event=" << e.event_no << " "
              << e.message << "\n";
  }
  std::cout << "SUMMARY published=" << evaluator.published().size()
            << " errors=" << evaluator.errors().size() << "\n";
  return 0;
}
