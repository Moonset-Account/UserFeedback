#include "event_evaluator.h"

#include <utility>

namespace policy {

DepthLimitedEvaluator::DepthLimitedEvaluator(std::size_t max_depth)
    : max_depth_(max_depth) {}

bool DepthLimitedEvaluator::begin_document(std::string doc_id) {
  if (doc_active_) {
    return false;  // previous document must be ended or cancelled first
  }
  reset_active();
  doc_active_ = true;
  doc_id_ = std::move(doc_id);
  return true;
}

FeedStatus DepthLimitedEvaluator::feed(const std::vector<Event>& batch) {
  if (paused_) {
    return FeedStatus::Paused;  // consume nothing while paused
  }
  if (!doc_active_) {
    return FeedStatus::NoDocument;
  }
  if (doc_failed_) {
    return FeedStatus::Failed;  // reject further input for a failed document
  }

  for (const Event& ev : batch) {
    switch (ev.type) {
      case EventType::EnterContainer: {
        // Depth budget check happens BEFORE the container is entered, so a
        // rejected enter leaves depth and container stack untouched.
        if (depth_ + 1 > max_depth_) {
          fail_active("depth limit exceeded: entering container '" + ev.name +
                      "' would reach depth " + std::to_string(depth_ + 1) +
                      " (max " + std::to_string(max_depth_) + ")");
          return FeedStatus::Failed;
        }
        container_stack_.push_back(ev.name);
        ++depth_;
        break;
      }
      case EventType::FieldRead: {
        std::string path;
        for (const std::string& segment : container_stack_) {
          path += '/';
          path += segment;
        }
        path += '/';
        path += ev.name;
        pending_fields_.push_back(FieldRecord{std::move(path), ev.value});
        break;
      }
      case EventType::ExitContainer: {
        if (depth_ == 0) {
          fail_active("unbalanced container exit at document root");
          return FeedStatus::Failed;
        }
        container_stack_.pop_back();
        --depth_;
        break;
      }
    }
    ++event_index_;  // only fully consumed events advance the numbering
  }
  return FeedStatus::Ok;
}

bool DepthLimitedEvaluator::end_document() {
  if (!doc_active_) {
    return false;
  }
  bool ok = !doc_failed_;
  if (ok && depth_ != 0) {
    fail_active("document ended with " + std::to_string(depth_) +
                " container(s) still open");
    ok = false;
  }
  if (ok) {
    published_.push_back(PublishedPolicy{doc_id_, std::move(pending_fields_)});
  }
  // Whether published or not, all per-document state is discarded so a
  // failed document can never pollute the next one.
  reset_active();
  return ok;
}

void DepthLimitedEvaluator::cancel() { reset_active(); }

void DepthLimitedEvaluator::pause() { paused_ = true; }

void DepthLimitedEvaluator::resume() { paused_ = false; }

bool DepthLimitedEvaluator::has_active_document() const { return doc_active_; }

bool DepthLimitedEvaluator::paused() const { return paused_; }

bool DepthLimitedEvaluator::current_failed() const { return doc_failed_; }

std::size_t DepthLimitedEvaluator::current_depth() const { return depth_; }

std::size_t DepthLimitedEvaluator::current_event_index() const {
  return event_index_;
}

std::size_t DepthLimitedEvaluator::max_depth() const { return max_depth_; }

void DepthLimitedEvaluator::fail_active(const std::string& message) {
  doc_failed_ = true;
  errors_.push_back(EvalError{doc_id_, event_index_, message});
}

void DepthLimitedEvaluator::reset_active() {
  doc_active_ = false;
  doc_failed_ = false;
  doc_id_.clear();
  depth_ = 0;
  event_index_ = 0;
  container_stack_.clear();
  pending_fields_.clear();
}

}  // namespace policy
