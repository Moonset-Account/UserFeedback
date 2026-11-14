// Command line front-end for the Mach-O offline mitigation inspector.
//
// Usage: macho_inspect <file> [<file> ...]
//
// Exit code (bit field):
//   0 clean / only INDETERMINATE findings,
//   1 at least one policy VIOLATION,
//   2 at least one CORRUPTION,
//   3 both violation and corruption.
// A file that cannot be opened is reported on stderr and contributes
// exit bit 2 as well.
#include <iostream>

#include "macho_inspect.h"

using macho_inspect::InspectionReport;
using macho_inspect::inspect_file;
using macho_inspect::render_report;

static const char kUsagePrefix[] = "usage: ";
static const char kUsageSuffix[] = " <mach-o-file> [more-files...]";
static const char kErrorPrefix[] = "error: ";
static const char kNewline = '\n';

int main(int argc, char **argv)
{
  if (argc < 2)
  {
    std::cerr << kUsagePrefix << argv[0] << kUsageSuffix << kNewline;
    return 64;
  }

  int exit_code = 0;
  for (int i = 1; i < argc; ++i)
  {
    try
    {
      InspectionReport report = inspect_file(argv[i]);
      std::cout << render_report(std::move(report));
      if (report.has_violation()) exit_code |= 1;
      if (report.has_corruption()) exit_code |= 2;
    }
    catch (const std::exception &ex)
    {
      std::cerr << kErrorPrefix << ex.what() << kNewline;
      exit_code |= 2;
    }
    if (i + 1 < argc) std::cout << kNewline;
  }
  return exit_code;
}
