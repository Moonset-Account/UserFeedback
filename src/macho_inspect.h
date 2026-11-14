// Mach-O offline mitigation inspector (SEC-VULN-011).
//
// Parses thin Mach-O and universal (fat) binaries without loading or
// executing any of their code. For every architecture slice it reports:
//   * the Mach-O file type,
//   * whether the position-independent executable (PIE) flag is present,
//   * whether a code-signature load command (LC_CODE_SIGNATURE) exists and
//     whether its payload (dataoff, datasize) lies fully inside the file.
//
// Findings are separated into three buckets:
//   VIOLATION     - a policy rule (R001/R002/R003) is not satisfied,
//   CORRUPTION    - the Mach-O / fat container structure is malformed,
//   INDETERMINATE - the required properties cannot be decided (unknown
//                   file type / unrecognised container).
//
// Signature inspection is deliberately limited to the LC_CODE_SIGNATURE
// payload metadata; the signature blob itself (SuperBlob/CMS) is never
// parsed or cryptographically validated.
#ifndef MACHO_INSPECT_H
#define MACHO_INSPECT_H

#include <cstdint>
#include <string>
#include <vector>

namespace macho_inspect {

enum class Severity {
  Violation,
  Corruption,
  Indeterminate,
};

struct Finding {
  Severity severity;           // policy violation / corruption / undecidable
  std::string rule_id;         // e.g. R001, F002, U001
  std::string message;         // human-readable explanation
  uint64_t evidence_offset = 0; // absolute offset in the inspected file
  uint64_t range_start = 0;     // start of the offending byte range
  uint64_t range_length = 0;    // length of the offending byte range
};

struct SliceReport {
  std::string arch;             // e.g. arm64, x86_64, armv7, unknown(cputype=..)
  bool slice_inspected = false; // false if its descriptor was unreadable
  uint32_t file_type = 0;       // mh_filetype (only when inspected)
  std::string file_type_name;   // e.g. MH_EXECUTE, MH_DYLIB
  uint32_t header_flags = 0;    // raw Mach-O header flags
  bool pie = false;             // MH_PIE (0x200000) set in header flags
  bool pie_policy_applies = false; // executables only
  bool has_signature_load = false; // LC_CODE_SIGNATURE present
  bool signature_payload_in_bounds = false; // dataoff/datasize fully in file
  uint64_t sig_payload_offset = 0; // absolute offset of signature payload
  uint64_t sig_payload_size = 0;
  uint64_t slice_offset = 0;    // absolute start of the slice in the file
  uint64_t slice_size = 0;
  std::vector<Finding> findings;
};

struct InspectionReport {
  std::string path;
  bool fat = false;
  std::vector<SliceReport> slices;
  std::vector<Finding> container_findings; // fat-container level findings

  bool has_violation() const;
  bool has_corruption() const;
  bool has_indeterminate() const;
};

// Reads the whole file into memory and inspects it. Throws std::runtime_error
// only when the file cannot be opened/read at all.
InspectionReport inspect_file(const std::string &path);

// Renders the report in the stable, line-oriented text format documented in
// README.md (one line per finding plus a per-slice property block).
std::string render_report(const InspectionReport &report);

} // namespace macho_inspect

#endif // MACHO_INSPECT_H
