// Mach-O offline mitigation inspector (SEC-VULN-011).
#include "macho_inspect.h"

#include <cstring>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace macho_inspect {
namespace {

constexpr uint32_t MH_MAGIC    = 0xfeedface;
constexpr uint32_t MH_CIGAM    = 0xcefaedfe;
constexpr uint32_t MH_MAGIC_64 = 0xfeedfacf;
constexpr uint32_t MH_CIGAM_64 = 0xcffaedfe;

constexpr uint32_t FAT_MAGIC    = 0xcafebabe;
constexpr uint32_t FAT_CIGAM    = 0xbebafeca;
constexpr uint32_t FAT_MAGIC_64 = 0xcafebabf;
constexpr uint32_t FAT_CIGAM_64 = 0xbfbafeca;

constexpr uint32_t LC_CODE_SIGNATURE = 0x1d;

constexpr uint32_t MH_PIE = 0x00200000u;

constexpr uint32_t MH_OBJECT      = 1;
constexpr uint32_t MH_EXECUTE     = 2;
constexpr uint32_t MH_FVMLIB      = 3;
constexpr uint32_t MH_CORE        = 4;
constexpr uint32_t MH_DYLIB       = 6;
constexpr uint32_t MH_DYLINKER    = 7;
constexpr uint32_t MH_BUNDLE      = 8;
constexpr uint32_t MH_DYLIB_STUB  = 9;
constexpr uint32_t MH_DSYM        = 10;
constexpr uint32_t MH_KEXT_BUNDLE = 11;
constexpr uint32_t MH_FILESET     = 12;

constexpr uint32_t CPU_ARCH_ABI64   = 0x01000000u;
constexpr uint32_t CPU_TYPE_X86     = 7;
constexpr uint32_t CPU_TYPE_X86_64  = 7 | CPU_ARCH_ABI64;
constexpr uint32_t CPU_TYPE_ARM     = 12;
constexpr uint32_t CPU_TYPE_ARM64   = 12 | CPU_ARCH_ABI64;
constexpr uint32_t CPU_TYPE_POWERPC = 18;
constexpr uint32_t CPU_TYPE_POWERPC64 = 18 | CPU_ARCH_ABI64;

uint32_t bswap32(uint32_t v) { return __builtin_bswap32(v); }
uint64_t bswap64(uint64_t v) { return __builtin_bswap64(v); }

class Region {
 public:
  Region(const uint8_t *data, size_t size) : data_(data), size_(size) {}

  bool read_u32(uint64_t off, bool swap, uint32_t *out) const {
    if (off > size_ || size_ - off < sizeof(uint32_t)) return false;
    uint32_t v;
    std::memcpy(&v, data_ + off, sizeof(v));
    *out = swap ? bswap32(v) : v;
    return true;
  }

  bool read_u64(uint64_t off, bool swap, uint64_t *out) const {
    if (off > size_ || size_ - off < sizeof(uint64_t)) return false;
    uint64_t v;
    std::memcpy(&v, data_ + off, sizeof(v));
    *out = swap ? bswap64(v) : v;
    return true;
  }

  bool contains(uint64_t off, uint64_t len) const {
    if (off > size_) return false;
    return len <= size_ - off;
  }

  size_t size() const { return size_; }

 private:
  const uint8_t *data_;
  size_t size_;
};

const char kUnknownCpu[] = "unknown-cputype-0x";
const char kI386[] = "i386";
const char kX8664[] = "x86_64";
const char kArmv7[] = "armv7";
const char kArm64[] = "arm64";
const char kPpc[] = "ppc";
const char kPpc64[] = "ppc64";

std::string hex_name(const char *prefix, uint32_t value)
{
  std::ostringstream os;
  os << prefix << std::hex << value;
  return os.str();
}

std::string arch_name(uint32_t cputype, uint32_t cpusubtype)
{
  (void)cpusubtype;
  if (cputype == CPU_TYPE_X86) return std::string(kI386);
  if (cputype == CPU_TYPE_X86_64) return std::string(kX8664);
  if (cputype == CPU_TYPE_ARM) return std::string(kArmv7);
  if (cputype == CPU_TYPE_ARM64) return std::string(kArm64);
  if (cputype == CPU_TYPE_POWERPC) return std::string(kPpc);
  if (cputype == CPU_TYPE_POWERPC64) return std::string(kPpc64);
  return hex_name(kUnknownCpu, cputype);
}

const char kUnknownFt[] = "unknown-filetype-0x";
const char kFtObject[] = "MH_OBJECT";
const char kFtExecute[] = "MH_EXECUTE";
const char kFtFvmlib[] = "MH_FVMLIB";
const char kFtCore[] = "MH_CORE";
const char kFtDylib[] = "MH_DYLIB";
const char kFtDylinker[] = "MH_DYLINKER";
const char kFtBundle[] = "MH_BUNDLE";
const char kFtDylibStub[] = "MH_DYLIB_STUB";
const char kFtDsym[] = "MH_DSYM";
const char kFtKext[] = "MH_KEXT_BUNDLE";
const char kFtFileset[] = "MH_FILESET";

std::string filetype_name(uint32_t ft)
{
  if (ft == MH_OBJECT) return std::string(kFtObject);
  if (ft == MH_EXECUTE) return std::string(kFtExecute);
  if (ft == MH_FVMLIB) return std::string(kFtFvmlib);
  if (ft == MH_CORE) return std::string(kFtCore);
  if (ft == MH_DYLIB) return std::string(kFtDylib);
  if (ft == MH_DYLINKER) return std::string(kFtDylinker);
  if (ft == MH_BUNDLE) return std::string(kFtBundle);
  if (ft == MH_DYLIB_STUB) return std::string(kFtDylibStub);
  if (ft == MH_DSYM) return std::string(kFtDsym);
  if (ft == MH_KEXT_BUNDLE) return std::string(kFtKext);
  if (ft == MH_FILESET) return std::string(kFtFileset);
  return hex_name(kUnknownFt, ft);
}

Finding make_finding(Severity sev, const std::string &rule,
                     const std::string &msg, uint64_t ev_off,
                     uint64_t range_start, uint64_t range_len)
{
  Finding f;
  f.severity = sev;
  f.rule_id = rule;
  f.message = msg;
  f.range_start = range_start;
  f.range_length = range_len;
  f.evidence_offset = ev_off;
  return f;
}

bool overflow_end(uint64_t start, uint64_t len, uint64_t *end)
{
  return __builtin_add_overflow(start, len, end);
}

const char kRuleF001[] = "F001";
const char kRuleF002[] = "F002";
const char kRuleF003[] = "F003";
const char kRuleF004[] = "F004";
const char kRuleR001[] = "R001";
const char kRuleR002[] = "R002";
const char kRuleR003[] = "R003";
const char kRuleU001[] = "U001";
const char kUnknownArch[] = "unknown";

const char kMsgTruncMagic[] =
    "truncated Mach-O: 4-byte magic not readable at slice start";
const char kMsgBadMagic[] = "bad Mach-O magic in slice";
const char kMsgTruncHeader[] = "truncated Mach-O header";
const char kMsgLcOverflow[] =
    "load-command table (sizeofcmds) extends past end of file";
const char kMsgLcCmdsize[] =
    "load command has invalid/truncated cmdsize";
const char kMsgLcSigSmall[] =
    "LC_CODE_SIGNATURE command smaller than 16 bytes";
const char kMsgSigOob[] =
    "LC_CODE_SIGNATURE payload offset/length outside the file";
const char kMsgNoPolicy[] =
    "no application/dylib policy defined for file type ";
const char kMsgNoPie[] =
    "MH_EXECUTE missing MH_PIE flag (position-independent executable required)";
const char kMsgNoSig[] =
    "LC_CODE_SIGNATURE load command missing (no signature payload record)";

SliceReport inspect_macho(const Region &file, uint64_t slice_off)
{
  SliceReport rep;
  rep.slice_offset = slice_off;

  uint32_t magic = 0;
  if (!file.read_u32(slice_off, false, &magic))
  {
    rep.arch = std::string(kUnknownArch);
    rep.findings.push_back(make_finding(
        Severity::Corruption, kRuleF001, kMsgTruncMagic,
        slice_off, slice_off, 4));
    return rep;
  }

  bool is64 = false;
  bool swap = false;
  if (magic == MH_MAGIC)
  {
    is64 = false; swap = false;
  }
  else if (magic == MH_CIGAM)
  {
    is64 = false; swap = true;
  }
  else if (magic == MH_MAGIC_64)
  {
    is64 = true; swap = false;
  }
  else if (magic == MH_CIGAM_64)
  {
    is64 = true; swap = true;
  }
  else
  {
    rep.arch = std::string(kUnknownArch);
    rep.findings.push_back(make_finding(
        Severity::Corruption, kRuleF001, kMsgBadMagic,
        slice_off, slice_off, 4));
    return rep;
  }

  const uint64_t header_size = is64 ? 32 : 28;
  if (!file.contains(slice_off, header_size))
  {
    rep.arch = std::string(kUnknownArch);
    rep.findings.push_back(make_finding(
        Severity::Corruption, kRuleF001, kMsgTruncHeader,
        slice_off, slice_off, header_size));
    return rep;
  }

  uint32_t cputype = 0, cpusubtype = 0, filetype = 0, ncmds = 0;
  uint32_t sizeofcmds = 0, flags = 0;
  file.read_u32(slice_off + 4, swap, &cputype);
  file.read_u32(slice_off + 8, swap, &cpusubtype);
  file.read_u32(slice_off + 12, swap, &filetype);
  file.read_u32(slice_off + 16, swap, &ncmds);
  file.read_u32(slice_off + 20, swap, &sizeofcmds);
  file.read_u32(slice_off + 24, swap, &flags);

  rep.arch = arch_name(cputype, cpusubtype);
  rep.slice_inspected = true;
  rep.file_type = filetype;
  rep.file_type_name = filetype_name(filetype);
  rep.header_flags = flags;
  rep.pie = (flags & MH_PIE) != 0;

  const uint64_t lc_start = slice_off + header_size;
  uint64_t lc_end = 0;
  bool add_overflow = overflow_end(lc_start, sizeofcmds, &lc_end);
  bool lc_table_overflows = add_overflow ||
                            !file.contains(lc_start, sizeofcmds);
  if (lc_table_overflows)
  {
    uint64_t reported_len = add_overflow
        ? sizeofcmds
        : (file.size() > lc_start ? file.size() - lc_start : 0);
    rep.findings.push_back(make_finding(
        Severity::Corruption, kRuleF002, kMsgLcOverflow,
        slice_off + 20, lc_start, reported_len));
    return rep;
  }

  bool policy_target = (filetype == MH_EXECUTE || filetype == MH_DYLIB);
  rep.pie_policy_applies = (filetype == MH_EXECUTE);

  bool saw_signature = false;
  bool sig_in_bounds = false;
  uint64_t sig_off = 0, sig_size = 0;
  bool load_truncated = false;

  uint64_t cur = lc_start;
  for (uint32_t i = 0; i < ncmds; ++i)
  {
    if (cur + 8 > lc_end)
    {
      load_truncated = true;
      break;
    }
    uint32_t cmd = 0, cmdsize = 0;
    file.read_u32(cur, swap, &cmd);
    file.read_u32(cur + 4, swap, &cmdsize);
    if (cmdsize < 8 || cur + cmdsize > lc_end)
    {
      rep.findings.push_back(make_finding(
          Severity::Corruption, kRuleF003, kMsgLcCmdsize,
          cur + 4, cur, cmdsize < 8 ? 8 : cmdsize));
      load_truncated = true;
      break;
    }

    if (cmd == LC_CODE_SIGNATURE)
    {
      if (cmdsize < 16)
      {
        rep.findings.push_back(make_finding(
            Severity::Corruption, kRuleF003, kMsgLcSigSmall,
            cur, cur, cmdsize));
      }
      else
      {
        uint32_t dataoff = 0, datasize = 0;
        file.read_u32(cur + 8, swap, &dataoff);
        file.read_u32(cur + 12, swap, &datasize);
        saw_signature = true;
        uint64_t abs_off = 0;
        bool off_overflow = overflow_end(slice_off, dataoff, &abs_off);
        sig_off = abs_off;
        sig_size = datasize;
        sig_in_bounds = !off_overflow && datasize != 0 &&
                        file.contains(abs_off, datasize);
        if (!sig_in_bounds)
        {
          rep.findings.push_back(make_finding(
              Severity::Corruption, kRuleR003, kMsgSigOob,
              cur + 8, abs_off, datasize));
        }
      }
    }
    cur += cmdsize;
  }
  if (load_truncated)
  {
    return rep;
  }

  rep.has_signature_load = saw_signature;
  rep.signature_payload_in_bounds = sig_in_bounds;
  rep.sig_payload_offset = sig_off;
  rep.sig_payload_size = sig_size;
  rep.slice_size = file.size() - slice_off;

  if (!policy_target)
  {
    rep.findings.push_back(make_finding(
        Severity::Indeterminate, kRuleU001,
        std::string(kMsgNoPolicy) + rep.file_type_name,
        slice_off + 12, slice_off + 12, 4));
    return rep;
  }

  if (filetype == MH_EXECUTE && !rep.pie)
  {
    rep.findings.push_back(make_finding(
        Severity::Violation, kRuleR001, kMsgNoPie,
        slice_off + 24, slice_off + 24, 4));
  }

  if (!saw_signature)
  {
    rep.findings.push_back(make_finding(
        Severity::Violation, kRuleR002, kMsgNoSig,
        lc_start, lc_start, 0));
  }
  return rep;
}

const char kRuleU002[] = "U002";
const char kRuleU003[] = "U003";
const char kMsgFatTrunc[] =
    "fat architecture descriptor table truncated";
const char kMsgFatOob[] =
    "fat slice descriptor points outside the file";
const char kMsgEmpty[] = "empty file: no Mach-O magic present";
const char kMsgBadContainer[] =
    "unrecognised magic: neither Mach-O nor fat container";

void inspect_fat(const Region &file, bool fat64, bool swap,
                 InspectionReport *out)
{
  out->fat = true;
  const uint64_t fat_header_size = 8;
  uint32_t nfat = 0;
  file.read_u32(4, swap, &nfat);

  const uint64_t entry_size = fat64 ? 32 : 20;
  uint64_t table_bytes = uint64_t(nfat) * entry_size;
  uint64_t entries_end = 0;
  if (overflow_end(fat_header_size, table_bytes, &entries_end) ||
      !file.contains(fat_header_size, table_bytes))
  {
    out->container_findings.push_back(make_finding(
        Severity::Corruption, kRuleF004, kMsgFatTrunc,
        4, fat_header_size, table_bytes));
    return;
  }

  for (uint32_t i = 0; i < nfat; ++i)
  {
    const uint64_t e = fat_header_size + uint64_t(i) * entry_size;
    uint32_t cputype = 0, cpusubtype = 0;
    file.read_u32(e, swap, &cputype);
    file.read_u32(e + 4, swap, &cpusubtype);

    uint64_t offset = 0, size = 0;
    if (fat64)
    {
      uint64_t off64 = 0, size64 = 0;
      file.read_u64(e + 8, swap, &off64);
      file.read_u64(e + 16, swap, &size64);
      offset = off64;
      size = size64;
    }
    else
    {
      uint32_t off32 = 0, size32 = 0;
      file.read_u32(e + 8, swap, &off32);
      file.read_u32(e + 12, swap, &size32);
      file.read_u32(e + 16, swap, &size32);
      offset = off32;
      size = size32;
    }

    uint64_t dummy_end = 0;
    bool bad = overflow_end(offset, size, &dummy_end) ||
               !file.contains(offset, size) || size < 4 ||
               !file.contains(offset, 4);
    if (bad)
    {
      SliceReport rep;
      rep.arch = arch_name(cputype, cpusubtype);
      rep.slice_inspected = false;
      rep.slice_offset = offset;
      rep.slice_size = size;
      rep.findings.push_back(make_finding(
          Severity::Corruption, kRuleF004, kMsgFatOob,
          e + 8, offset, size));
      out->slices.push_back(rep);
      continue;
    }

    SliceReport rep = inspect_macho(file, offset);
    rep.slice_size = size;
    out->slices.push_back(rep);
  }
}

const char kSevViolation[] = "VIOLATION";
const char kSevCorruption[] = "CORRUPTION";
const char kSevIndeterminate[] = "INDETERMINATE";
const char kSevUnknown[] = "?";

const char *severity_name(Severity s)
{
  if (s == Severity::Violation) return kSevViolation;
  if (s == Severity::Corruption) return kSevCorruption;
  if (s == Severity::Indeterminate) return kSevIndeterminate;
  return kSevUnknown;
}

} // namespace

bool InspectionReport::has_violation() const
{
  for (const auto &s : slices)
    for (const auto &f : s.findings)
      if (f.severity == Severity::Violation) return true;
  for (const auto &f : container_findings)
    if (f.severity == Severity::Violation) return true;
  return false;
}

bool InspectionReport::has_corruption() const
{
  for (const auto &s : slices)
    for (const auto &f : s.findings)
      if (f.severity == Severity::Corruption) return true;
  for (const auto &f : container_findings)
    if (f.severity == Severity::Corruption) return true;
  return false;
}

bool InspectionReport::has_indeterminate() const
{
  for (const auto &s : slices)
    for (const auto &f : s.findings)
      if (f.severity == Severity::Indeterminate) return true;
  for (const auto &f : container_findings)
    if (f.severity == Severity::Indeterminate) return true;
  return false;
}

const char kMsgCannotOpen[] = "cannot open file: ";
const uint8_t kEmptyByte = 0;

InspectionReport inspect_file(const std::string &path)
{
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error(std::string(kMsgCannotOpen) + path);
  std::ostringstream ss;
  ss << in.rdbuf();
  std::string contents = ss.str();

  InspectionReport report;
  report.path = path;
  const uint8_t *data = contents.empty()
      ? &kEmptyByte
      : reinterpret_cast<const uint8_t *>(contents.data());
  Region file(data, contents.size());

  if (contents.empty())
  {
    report.container_findings.push_back(make_finding(
        Severity::Indeterminate, kRuleU002, kMsgEmpty, 0, 0, 0));
    return report;
  }

  uint32_t magic = 0;
  file.read_u32(0, false, &magic);

  if (magic == FAT_MAGIC)
  {
    inspect_fat(file, false, false, &report);
    return report;
  }
  if (magic == FAT_CIGAM)
  {
    inspect_fat(file, false, true, &report);
    return report;
  }
  if (magic == FAT_MAGIC_64)
  {
    inspect_fat(file, true, false, &report);
    return report;
  }
  if (magic == FAT_CIGAM_64)
  {
    inspect_fat(file, true, true, &report);
    return report;
  }
  if (magic == MH_MAGIC || magic == MH_CIGAM ||
      magic == MH_MAGIC_64 || magic == MH_CIGAM_64)
  {
    report.slices.push_back(inspect_macho(file, 0));
    return report;
  }

  report.container_findings.push_back(make_finding(
      Severity::Indeterminate, kRuleU003, kMsgBadContainer, 0, 0, 4));
  return report;
}

const char kLabelFile[] = "file: ";
const char kLabelContainer[] = "container: ";
const char kLabelThin[] = "thin Mach-O";
const char kLabelFat[] = "universal (fat)";
const char kLabelSlices[] = "slices: ";
const char kLabelSlice[] = "slice[";
const char kLabelArch[] = " arch=";
const char kLabelSliceOff[] = " slice_off=";
const char kLabelSliceSize[] = " slice_size=";
const char kLabelStatus[] = "  status=uninspected";
const char kLabelFiletype[] = "  filetype=";
const char kLabelFlags[] = " flags=0x";
const char kLabelPie[] = " pie=";
const char kLabelPiePolicy[] = " pie_policy=";
const char kLabelApplies[] = "applies";
const char kLabelNa[] = "n/a";
const char kLabelYes[] = "yes";
const char kLabelNo[] = "no";
const char kLabelCsLoad[] = "  code_signature_load=";
const char kLabelPresent[] = "present";
const char kLabelAbsent[] = "absent";
const char kLabelInBounds[] = " payload_in_bounds=";
const char kLabelPayloadOff[] = " payload_off=";
const char kLabelPayloadSize[] = " payload_size=";
const char kLabelEvidence[] = " evidence_off=";
const char kLabelRange[] = " range=[";
const char kLabelRangeSep[] = ",+";
const char kLabelDash[] = ") - ";
const char kLabelIndent[] = "  ";
const char kLabelBracket[] = "]";

const char kNewline = '\n';
const char kSpace = ' ';

void append_finding(std::ostringstream &os, const Finding &f)
{
  os << severity_name(f.severity) << kSpace << f.rule_id
     << kLabelEvidence << f.evidence_offset
     << kLabelRange << f.range_start << kLabelRangeSep
     << f.range_length << kLabelDash << f.message << kNewline;
}

std::string render_report(const InspectionReport &report)
{
  std::ostringstream os;
  os << kLabelFile << report.path << kNewline;
  os << kLabelContainer << (report.fat ? kLabelFat : kLabelThin) << kNewline;
  os << kLabelSlices << report.slices.size() << kNewline;

  for (const auto &f : report.container_findings)
  {
    append_finding(os, f);
  }

  for (size_t i = 0; i < report.slices.size(); ++i)
  {
    const SliceReport &s = report.slices[i];
    os << kLabelSlice << i << kLabelBracket << kLabelArch << s.arch
       << kLabelSliceOff << s.slice_offset
       << kLabelSliceSize << s.slice_size << kNewline;
    if (!s.slice_inspected)
    {
      os << kLabelStatus << kNewline;
    }
    else
    {
      os << kLabelFiletype << s.file_type_name
         << kLabelFlags << std::hex << s.header_flags << std::dec
         << kLabelPie << (s.pie ? kLabelYes : kLabelNo)
         << kLabelPiePolicy
         << (s.pie_policy_applies ? kLabelApplies : kLabelNa)
         << kNewline;
      os << kLabelCsLoad
         << (s.has_signature_load ? kLabelPresent : kLabelAbsent)
         << kLabelInBounds
         << (s.signature_payload_in_bounds ? kLabelYes : kLabelNo)
         << kLabelPayloadOff << s.sig_payload_offset
         << kLabelPayloadSize << s.sig_payload_size << kNewline;
    }
    for (const auto &f : s.findings)
    {
      os << kLabelIndent;
      append_finding(os, f);
    }
  }
  return os.str();
}

} // namespace macho_inspect
