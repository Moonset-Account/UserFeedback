#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include "../src/macho_inspect.h"

using namespace macho_inspect;

namespace {

int g_checks = 0;
int g_failures = 0;
std::string g_current_case;
const char kNewline = 0x0a;

void begin_case(const std::string &case_id)
{
  g_current_case = case_id;
  std::cout << case_id << kNewline;
}

const char kFailLabel[] = "  FAIL [";
const char kFailMid[] = "] ";
const char kFailAt[] = " at ";
const char kColon[] = ":";
const char kCheckExpr[] = "check";

void record(bool ok, const char *expr, const char *file, int line)
{
  ++g_checks;
  if (!ok)
  {
    ++g_failures;
    std::cout << kFailLabel << g_current_case << kFailMid << expr
              << kFailAt << file << kColon << line << kNewline;
  }
}

#define CHECK(cond) record((cond), kCheckExpr, __FILE__, __LINE__)

struct Blob
{
  std::vector<uint8_t> b;

  void u8(uint8_t v) { b.push_back(v); }
  void u32(uint32_t v)
  {
    for (int i = 0; i < 4; ++i) b.push_back(uint8_t(v >> (8 * i)));
  }
  void u64(uint64_t v)
  {
    for (int i = 0; i < 8; ++i) b.push_back(uint8_t(v >> (8 * i)));
  }
  void pad(size_t n) { b.resize(b.size() + n, 0); }
  void put_u32(size_t off, uint32_t v)
  {
    for (int i = 0; i < 4; ++i) b[off + i] = uint8_t(v >> (8 * i));
  }
  size_t size() const { return b.size(); }

  void save(const std::string &path) const
  {
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out.write(reinterpret_cast<const char *>(b.data()),
              static_cast<std::streamsize>(b.size()));
  }
};

const uint32_t MAGIC_64 = 0xfeedfacf;
const uint32_t FAT_MAGIC = 0xcafebabe;
const uint32_t LC_CODE_SIGNATURE = 0x1d;
const uint32_t MH_PIE = 0x200000;
const uint32_t FT_EXECUTE = 2;
const uint32_t FT_DYLIB = 6;
const uint32_t FT_OBJECT = 1;
const uint32_t CPU_X86_64 = 0x01000007;
const uint32_t CPU_ARM64 = 0x0100000c;
const uint32_t CPU_ARM = 12;

// Minimal 64-bit thin Mach-O; signature command references an 8-byte
// payload at the image end when in-bounds.
Blob build_thin(uint32_t cputype, uint32_t filetype, uint32_t flags,
                bool with_sig, bool sig_in_bounds)
{
  Blob bl;
  const uint32_t ncmds = with_sig ? 1u : 0u;
  const uint32_t sizeofcmds = with_sig ? 16u : 0u;
  const uint32_t payload_size = 8;
  const uint32_t header_size = 32;
  const uint32_t dataoff = with_sig ? header_size + sizeofcmds : 0;

  bl.u32(MAGIC_64);
  bl.u32(cputype);
  bl.u32(0);
  bl.u32(filetype);
  bl.u32(ncmds);
  bl.u32(sizeofcmds);
  bl.u32(flags);
  bl.u32(0);

  if (with_sig)
  {
    bl.u32(LC_CODE_SIGNATURE);
    bl.u32(16);
    bl.u32(dataoff);
    if (sig_in_bounds)
    {
      bl.u32(payload_size);
      bl.pad(payload_size);
    }
    else
    {
      bl.u32(0x4000);
    }
  }
  return bl;
}

struct FatEntry
{
  uint32_t cputype;
  const Blob *image;
  bool force_out_of_bounds = false;
  uint64_t oob_offset = 0;
  uint64_t oob_size = 0;
};

// Big-endian fat (universal) container wrapping the given slice images.
Blob build_fat(const std::vector<FatEntry> &entries)
{
  Blob bl;
  auto be32 = [&](uint32_t v)
  {
    for (int i = 3; i >= 0; --i) bl.u8(uint8_t(v >> (8 * i)));
  };

  const uint32_t header = 8;
  const uint32_t entry_size = 20;
  uint32_t offset = header +
      entry_size * static_cast<uint32_t>(entries.size());

  be32(FAT_MAGIC);
  be32(static_cast<uint32_t>(entries.size()));
  for (const auto &e : entries)
  {
    be32(e.cputype);
    be32(0);
    if (e.force_out_of_bounds)
    {
      be32(static_cast<uint32_t>(e.oob_offset));
      be32(0);
      be32(static_cast<uint32_t>(e.oob_size));
    }
    else
    {
      be32(offset);
      be32(0);
      be32(static_cast<uint32_t>(e.image->size()));
      offset += static_cast<uint32_t>(e.image->size());
    }
  }
  for (const auto &e : entries)
  {
    if (!e.force_out_of_bounds)
    {
      bl.b.insert(bl.b.end(), e.image->b.begin(), e.image->b.end());
    }
  }
  return bl;
}

const char kFixturePrefix[] = "build/fixture_";
const char kFixtureSuffix[] = ".bin";
const char kArchX8664[] = "x86_64";
const char kArchArm64[] = "arm64";
const char kFtExecuteName[] = "MH_EXECUTE";
const char kRuleR001[] = "R001";
const char kRuleR002[] = "R002";
const char kRuleR003[] = "R003";
const char kRuleF002[] = "F002";
const char kRuleF003[] = "F003";
const char kRuleF004[] = "F004";
const char kRuleF001[] = "F001";
const char kRuleU001[] = "U001";
const char kRuleU003[] = "U003";

const char kIdThinOk[] = "TC01-thin-exec-compliant";
const char kIdFatMissing[] = "TC02-fat-one-slice-missing-pie";
const char kIdLcTrunc[] = "TC03-truncated-load-command";
const char kIdFatOob[] = "TC04-fat-slice-descriptor-out-of-bounds";
const char kIdSigOob[] = "TC05-signature-payload-out-of-bounds";
const char kIdDylibOk[] = "TC06-dylib-no-pie-valid-signature";
const char kIdThinNoPieNoSig[] = "TC07-thin-exec-missing-pie-and-signature";
const char kIdUnknownType[] = "TC08-object-file-no-policy";
const char kIdGarbage[] = "TC09-unrecognised-magic";
const char kIdTruncHeader[] = "TC10-truncated-macho-header";
const char kIdFatTrunc[] = "TC11-fat-descriptor-table-truncated";
const char kIdLcTableOob[] = "TC12-sizeofcmds-extends-past-eof";

std::string tmp_path(const std::string &case_id)
{
  return std::string(kFixturePrefix) + case_id + std::string(kFixtureSuffix);
}

InspectionReport inspect_blob(const std::string &case_id, const Blob &bl)
{
  std::string p = tmp_path(case_id);
  bl.save(p);
  return inspect_file(p);
}

bool has_finding(const SliceReport &s, const char *rule)
{
  for (const auto &f : s.findings)
    if (f.rule_id == rule) return true;
  return false;
}

const Finding *find_finding(const SliceReport &s, const char *rule)
{
  for (const auto &f : s.findings)
    if (f.rule_id == rule) return &f;
  return nullptr;
}

void test_thin_compliant()
{
  begin_case(kIdThinOk);
  Blob bl = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  InspectionReport r = inspect_blob(kIdThinOk, bl);
  CHECK(!r.fat);
  CHECK(r.slices.size() == 1);
  const SliceReport &s = r.slices[0];
  CHECK(s.slice_inspected);
  CHECK(s.arch == kArchX8664);
  CHECK(s.file_type == FT_EXECUTE);
  CHECK(s.file_type_name == kFtExecuteName);
  CHECK(s.pie);
  CHECK(s.pie_policy_applies);
  CHECK(s.has_signature_load);
  CHECK(s.signature_payload_in_bounds);
  CHECK(s.sig_payload_size == 8);
  CHECK(s.findings.empty());
  CHECK(!r.has_violation() && !r.has_corruption() && !r.has_indeterminate());
}

void test_fat_one_slice_missing_pie()
{
  begin_case(kIdFatMissing);
  Blob good = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  Blob bad = build_thin(CPU_ARM64, FT_EXECUTE, 0, false, true);
  std::vector<FatEntry> entries;
  entries.push_back(FatEntry{CPU_X86_64, &good});
  entries.push_back(FatEntry{CPU_ARM64, &bad});
  Blob fat = build_fat(entries);
  InspectionReport r = inspect_blob(kIdFatMissing, fat);

  CHECK(r.fat);
  CHECK(r.slices.size() == 2);
  CHECK(r.slices[0].arch == kArchX8664);
  CHECK(r.slices[0].pie);
  CHECK(r.slices[0].findings.empty());

  const SliceReport &arm = r.slices[1];
  CHECK(arm.arch == kArchArm64);
  CHECK(arm.slice_inspected);
  CHECK(!arm.pie);
  CHECK(arm.pie_policy_applies);
  CHECK(!arm.has_signature_load);
  const Finding *r1 = find_finding(arm, kRuleR001);
  CHECK(r1 != nullptr);
  if (r1)
  {
    CHECK(r1->severity == Severity::Violation);
    CHECK(r1->evidence_offset == arm.slice_offset + 24);
    CHECK(r1->range_start == arm.slice_offset + 24);
    CHECK(r1->range_length == 4);
  }
  const Finding *r2 = find_finding(arm, kRuleR002);
  CHECK(r2 != nullptr);
  if (r2) CHECK(r2->severity == Severity::Violation);
  CHECK(r.has_violation());
  CHECK(!r.has_corruption());
}

void test_truncated_load_command()
{
  begin_case(kIdLcTrunc);
  Blob bl = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  // The 16-byte signature command is fully present in the file but the
  // header declares sizeofcmds=8, so the command body extends past the
  // declared load-command table: a truncated/invalid load command.
  bl.put_u32(20, 8);
  InspectionReport r = inspect_blob(kIdLcTrunc, bl);

  CHECK(r.slices.size() == 1);
  const SliceReport &s = r.slices[0];
  CHECK(s.slice_inspected);
  const Finding *f = find_finding(s, kRuleF003);
  CHECK(f != nullptr);
  if (f)
  {
    CHECK(f->severity == Severity::Corruption);
    // cmdsize field is at load-command start +4; command starts at 32.
    CHECK(f->evidence_offset == 32 + 4);
    CHECK(f->range_start == 32);
    CHECK(f->range_length == 16);
  }
  CHECK(r.has_corruption());
  CHECK(!r.has_violation());
}

void test_fat_slice_descriptor_out_of_bounds()
{
  begin_case(kIdFatOob);
  Blob good = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  std::vector<FatEntry> entries;
  entries.push_back(FatEntry{CPU_X86_64, &good});
  FatEntry bad;
  bad.cputype = CPU_ARM;
  bad.image = nullptr;
  bad.force_out_of_bounds = true;
  bad.oob_offset = 0x00100000;
  bad.oob_size = 0x1000;
  entries.push_back(bad);
  Blob fat = build_fat(entries);
  InspectionReport r = inspect_blob(kIdFatOob, fat);

  CHECK(r.fat);
  CHECK(r.slices.size() == 2);
  CHECK(r.slices[0].findings.empty());
  const SliceReport &s = r.slices[1];
  CHECK(!s.slice_inspected);
  const Finding *f = find_finding(s, kRuleF004);
  CHECK(f != nullptr);
  if (f)
  {
    CHECK(f->severity == Severity::Corruption);
    // Second descriptor starts at 8 + 20 = 28; offset field at +8.
    CHECK(f->evidence_offset == 28 + 8);
    CHECK(f->range_start == 0x00100000);
    CHECK(f->range_length == 0x1000);
  }
  CHECK(r.has_corruption());
}

void test_signature_payload_out_of_bounds()
{
  begin_case(kIdSigOob);
  Blob bl = build_thin(CPU_ARM64, FT_DYLIB, 0, true, false);
  InspectionReport r = inspect_blob(kIdSigOob, bl);
  const SliceReport &s = r.slices[0];
  CHECK(s.has_signature_load);
  CHECK(!s.signature_payload_in_bounds);
  const Finding *f = find_finding(s, kRuleR003);
  CHECK(f != nullptr);
  if (f)
  {
    CHECK(f->severity == Severity::Corruption);
    // dataoff field is inside the signature command at 32+8.
    CHECK(f->evidence_offset == 32 + 8);
    CHECK(f->range_length == 0x4000);
  }
  CHECK(r.has_corruption());
  CHECK(!r.has_violation());
}

void test_dylib_no_pie_required()
{
  begin_case(kIdDylibOk);
  Blob bl = build_thin(CPU_ARM64, FT_DYLIB, 0, true, true);
  InspectionReport r = inspect_blob(kIdDylibOk, bl);
  const SliceReport &s = r.slices[0];
  CHECK(s.file_type == FT_DYLIB);
  CHECK(!s.pie_policy_applies);
  CHECK(!s.pie);
  CHECK(s.has_signature_load);
  CHECK(s.signature_payload_in_bounds);
  CHECK(s.findings.empty());
}

void test_thin_missing_pie_and_signature()
{
  begin_case(kIdThinNoPieNoSig);
  Blob bl = build_thin(CPU_X86_64, FT_EXECUTE, 0, false, true);
  InspectionReport r = inspect_blob(kIdThinNoPieNoSig, bl);
  const SliceReport &s = r.slices[0];
  CHECK(!s.pie);
  CHECK(!s.has_signature_load);
  CHECK(find_finding(s, kRuleR001) != nullptr);
  CHECK(find_finding(s, kRuleR002) != nullptr);
  CHECK(r.has_violation());
}

void test_object_file_indeterminate()
{
  begin_case(kIdUnknownType);
  Blob bl = build_thin(CPU_X86_64, FT_OBJECT, 0, false, true);
  InspectionReport r = inspect_blob(kIdUnknownType, bl);
  const SliceReport &s = r.slices[0];
  CHECK(s.file_type == FT_OBJECT);
  CHECK(!s.pie_policy_applies);
  const Finding *f = find_finding(s, kRuleU001);
  CHECK(f != nullptr);
  if (f)
  {
    CHECK(f->severity == Severity::Indeterminate);
    CHECK(f->evidence_offset == 12);
    CHECK(f->range_length == 4);
  }
  CHECK(r.has_indeterminate());
  CHECK(!r.has_violation());
  CHECK(!r.has_corruption());
}

void test_unrecognised_magic()
{
  begin_case(kIdGarbage);
  Blob bl;
  bl.u32(0xdeadbeef);
  bl.pad(16);
  InspectionReport r = inspect_blob(kIdGarbage, bl);
  CHECK(r.slices.empty());
  CHECK(r.container_findings.size() == 1);
  const Finding &f = r.container_findings[0];
  CHECK(f.severity == Severity::Indeterminate);
  CHECK(f.rule_id == kRuleU003);
  CHECK(r.has_indeterminate());
}

void test_truncated_header()
{
  begin_case(kIdTruncHeader);
  Blob bl = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  bl.b.resize(20); // smaller than the 32-byte 64-bit header
  InspectionReport r = inspect_blob(kIdTruncHeader, bl);
  const SliceReport &s = r.slices[0];
  CHECK(!s.slice_inspected);
  const Finding *f = find_finding(s, kRuleF001);
  CHECK(f != nullptr);
  if (f)
  {
    CHECK(f->severity == Severity::Corruption);
    CHECK(f->evidence_offset == 0);
  }
  CHECK(r.has_corruption());
}

void test_fat_descriptor_table_truncated()
{
  begin_case(kIdFatTrunc);
  Blob good = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  std::vector<FatEntry> entries;
  entries.push_back(FatEntry{CPU_X86_64, &good});
  Blob fat = build_fat(entries);
  // Claim three architectures while only one descriptor exists.
  fat.put_u32(4, 3);
  InspectionReport r = inspect_blob(kIdFatTrunc, fat);
  CHECK(r.fat);
  CHECK(r.container_findings.size() == 1);
  const Finding &f = r.container_findings[0];
  CHECK(f.severity == Severity::Corruption);
  CHECK(f.rule_id == kRuleF004);
  CHECK(f.evidence_offset == 4);
  CHECK(r.has_corruption());
}

const char kSummaryChecks[] = "checks=";
const char kSummaryFailures[] = " failures=";
const char kSummaryAllPass[] = "ALL TESTS PASSED";
const char kSummarySomeFail[] = "TESTS FAILED";


void test_sizeofcmds_overflow()
{
  begin_case(kIdLcTableOob);
  Blob bl = build_thin(CPU_X86_64, FT_EXECUTE, MH_PIE, true, true);
  bl.put_u32(20, 0x00010000); // sizeofcmds far beyond EOF
  InspectionReport r = inspect_blob(kIdLcTableOob, bl);
  const SliceReport &s = r.slices[0];
  CHECK(has_finding(s, kRuleF002));
  const Finding *f = find_finding(s, kRuleF002);
  if (f)
  {
    CHECK(f->severity == Severity::Corruption);
    CHECK(f->evidence_offset == 20);
    CHECK(f->range_start == 32);
  }
  CHECK(r.has_corruption());
}
} // namespace

int main()
{
  test_thin_compliant();
  test_fat_one_slice_missing_pie();
  test_truncated_load_command();
  test_fat_slice_descriptor_out_of_bounds();
  test_signature_payload_out_of_bounds();
  test_dylib_no_pie_required();
  test_thin_missing_pie_and_signature();
  test_object_file_indeterminate();
  test_unrecognised_magic();
  test_truncated_header();
  test_fat_descriptor_table_truncated();
  test_sizeofcmds_overflow();

  std::cout << kSummaryChecks << g_checks
            << kSummaryFailures << g_failures << kNewline;
  if (g_failures == 0)
  {
    std::cout << kSummaryAllPass << kNewline;
    return 0;
  }
  std::cout << kSummarySomeFail << kNewline;
  return 1;
}
