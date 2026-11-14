// Stable text rendering of a CoverageReport.
// Verdict lines always start with "V |" so downstream tooling and tests can
// parse them without depending on surrounding prose.
#pragma once

#include <ostream>

#include "ssp_checker/Analysis.h"

namespace ssp {

void renderReport(const CoverageReport& report, std::ostream& output);

}  // namespace ssp
