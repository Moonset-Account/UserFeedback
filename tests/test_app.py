#!/usr/bin/env python3
"""Unit tests for the DMARC aggregate analyzer (app.py)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

BEGIN = 1794528000   # 2026-11-13T00:00:00Z
END = 1794614399     # 2026-11-13T23:59:59Z
DATE = "2026-11-13"

REPORT_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<feedback>
  <report_metadata>
    <org_name>{org}</org_name>
    <email>noreply@reporter.example</email>
    <report_id>{rid}</report_id>
    <date_range><begin>{begin}</begin><end>{end}</end></date_range>
  </report_metadata>
  <policy_published>
    <domain>{domain}</domain>
    <adkim>r</adkim><aspf>r</aspf>
    <p>{policy}</p><sp>quarantine</sp><pct>100</pct>
  </policy_published>
{records}
</feedback>
"""

RECORD_TEMPLATE = """  <record>
    <row>
      <source_ip>{ip}</source_ip>
      <count>{count}</count>
      <policy_evaluated>
        <disposition>{disposition}</disposition>
        <dkim>{eval_dkim}</dkim>
        <spf>{eval_spf}</spf>
        {reasons}
      </policy_evaluated>
    </row>
    <identifiers><header_from>{header_from}</header_from></identifiers>
    <auth_results>
      <dkim><domain>{dkim_domain}</domain><selector>s1</selector><result>{dkim_result}</result></dkim>
      <spf><domain>{spf_domain}</domain><result>{spf_result}</result></spf>
    </auth_results>
  </record>
"""

REGISTRY = "ip,category,owner\n203.0.113.10,esp,MailVendor\n198.51.100.0/24,partner,PartnerCorp\n"


def make_record(ip="203.0.113.10", count=5, disposition="none",
                eval_dkim="pass", eval_spf="pass",
                header_from="example.com",
                dkim_domain="example.com", dkim_result="pass",
                spf_domain="example.com", spf_result="pass",
                reasons=""):
    return RECORD_TEMPLATE.format(
        ip=ip, count=count, disposition=disposition,
        eval_dkim=eval_dkim, eval_spf=eval_spf,
        header_from=header_from,
        dkim_domain=dkim_domain, dkim_result=dkim_result,
        spf_domain=spf_domain, spf_result=spf_result,
        reasons=reasons)


def make_report(org="ReporterOne", rid="r1", records=None, policy="quarantine",
                domain="example.com"):
    return REPORT_TEMPLATE.format(
        org=org, rid=rid, begin=BEGIN, end=END, domain=domain,
        policy=policy, records="".join(records or []))


class AnalyzerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry_path = self._write("registry.csv", REGISTRY)
        self.entries = app.load_registry(self.registry_path)

    def _write(self, name, content):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _report(self, name, **kwargs):
        return self._write(name, make_report(**kwargs))

    def _aggregate(self, paths):
        reports = [app.parse_report(p) for p in paths]
        kept, review = app.dedup_reports(reports)
        return app.rows_to_list(app.aggregate(kept, self.entries)), review

    # -- parsing / basic aggregation -------------------------------------

    def test_parse_report_fields(self):
        path = self._report("a.xml", records=[make_record()])
        rep = app.parse_report(path)
        self.assertEqual(rep["org"], "ReporterOne")
        self.assertEqual(rep["report_id"], "r1")
        self.assertEqual(rep["domain"], "example.com")
        self.assertEqual(rep["begin"], BEGIN)
        self.assertEqual(len(rep["records"]), 1)
        self.assertEqual(rep["records"][0]["count"], 5)
        self.assertEqual(rep["records"][0]["source_ip"], "203.0.113.10")

    def test_aggregate_registered_and_unknown_rows(self):
        records = [
            make_record(ip="203.0.113.10", count=5),
            make_record(ip="192.0.2.7", count=3, spf_result="fail",
                        eval_spf="fail", dkim_result="fail", eval_dkim="fail"),
        ]
        path = self._report("a.xml", records=records)
        rows, review = self._aggregate([path])
        self.assertEqual(review, [])
        self.assertEqual(len(rows), 2)
        by_cat = {r["category"]: r for r in rows}
        self.assertEqual(by_cat["esp"]["count"], 5)
        self.assertEqual(by_cat["esp"]["owners"], ["MailVendor"])
        self.assertEqual(by_cat["unknown"]["count"], 3)
        self.assertEqual(by_cat["esp"]["date"], DATE)
        self.assertEqual(by_cat["esp"]["domain"], "example.com")
        self.assertEqual(by_cat["esp"]["report_ids"], ["ReporterOne:r1"])

    def test_cidr_registry_match(self):
        path = self._report("a.xml", records=[make_record(ip="198.51.100.77")])
        rows, _ = self._aggregate([path])
        self.assertEqual(rows[0]["category"], "partner")

    # -- compliance rule ---------------------------------------------------

    def test_compliant_via_spf_aligned(self):
        path = self._report("a.xml", records=[make_record(
            dkim_result="fail", eval_dkim="fail")])
        rows, _ = self._aggregate([path])
        self.assertEqual(rows[0]["spf"], "pass_aligned")
        self.assertEqual(rows[0]["dkim"], "not_pass")
        self.assertTrue(rows[0]["compliant"])

    def test_compliant_via_dkim_only(self):
        path = self._report("a.xml", records=[make_record(
            spf_domain="mailing.example.net", spf_result="fail",
            eval_spf="fail")])
        rows, _ = self._aggregate([path])
        self.assertEqual(rows[0]["dkim"], "pass_aligned")
        self.assertEqual(rows[0]["spf"], "not_pass")
        self.assertTrue(rows[0]["compliant"])

    def test_not_compliant_when_neither_aligns(self):
        path = self._report("a.xml", records=[make_record(
            spf_domain="other.example.net", spf_result="pass",
            dkim_domain="other.example.net", dkim_result="pass")])
        rows, _ = self._aggregate([path])
        self.assertEqual(rows[0]["spf"], "pass_unaligned")
        self.assertEqual(rows[0]["dkim"], "pass_unaligned")
        self.assertFalse(rows[0]["compliant"])

    def test_relaxed_alignment_subdomain(self):
        self.assertTrue(app.domains_aligned("mail.example.com", "example.com"))
        self.assertTrue(app.domains_aligned("example.com", "mail.example.com"))
        self.assertTrue(app.domains_aligned("example.com", "example.com"))
        self.assertFalse(app.domains_aligned("evil-example.com", "example.com"))
        self.assertFalse(app.domains_aligned("example.com.evil.net", "example.com"))
        self.assertFalse(app.domains_aligned("", "example.com"))

    # -- forwarding marker -------------------------------------------------

    def test_forwarding_marker_from_reason(self):
        reason = ('<reason><type>forwarded</type>'
                  '<comment>listed forwarder</comment></reason>')
        path = self._report("a.xml", records=[make_record(
            spf_result="fail", eval_spf="fail",
            spf_domain="mailing.example.net", reasons=reason)])
        rows, _ = self._aggregate([path])
        self.assertTrue(rows[0]["forwarded"])
        self.assertTrue(rows[0]["relies_on_forwarding"])

    def test_forwarding_marker_from_signature(self):
        # No explicit reason, but SPF broken while DKIM passes aligned.
        path = self._report("a.xml", records=[make_record(
            spf_result="fail", eval_spf="fail",
            spf_domain="mailing.example.net")])
        rows, _ = self._aggregate([path])
        self.assertTrue(rows[0]["forwarded"])

    def test_no_forwarding_marker_when_spf_aligned(self):
        path = self._report("a.xml", records=[make_record()])
        rows, _ = self._aggregate([path])
        self.assertFalse(rows[0]["forwarded"])
        self.assertFalse(rows[0]["relies_on_forwarding"])

    def test_disposition_deviation_flag(self):
        path = self._report("a.xml", policy="reject",
                            records=[make_record(disposition="none")])
        rows, _ = self._aggregate([path])
        self.assertTrue(rows[0]["disposition_deviation"])
        self.assertEqual(rows[0]["policies"], ["reject"])

    # -- dedup / review ----------------------------------------------------

    def test_identical_resubmission_counted_once(self):
        content = make_report(records=[make_record(count=5)])
        p1 = self._write("a.xml", content)
        p2 = self._write("b.xml", content)
        rows, review = self._aggregate([p1, p2])
        self.assertEqual(review, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 5)

    def test_conflicting_resubmission_goes_to_review(self):
        p1 = self._write("a.xml", make_report(records=[make_record(count=5)]))
        p2 = self._write("b.xml", make_report(records=[make_record(count=9)]))
        rows, review = self._aggregate([p1, p2])
        self.assertEqual(rows, [])
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["org"], "ReporterOne")
        self.assertEqual(review[0]["report_id"], "r1")
        self.assertEqual(review[0]["files"], sorted([p1, p2]))

    def test_different_reporters_count_separately(self):
        p1 = self._write("a.xml", make_report(org="ReporterOne", rid="r1",
                                              records=[make_record(count=5)]))
        p2 = self._write("b.xml", make_report(org="ReporterTwo", rid="r1",
                                              records=[make_record(count=5)]))
        rows, review = self._aggregate([p1, p2])
        self.assertEqual(review, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 10)
        self.assertEqual(rows[0]["report_ids"],
                         ["ReporterOne:r1", "ReporterTwo:r1"])

    # -- summaries / traceability ------------------------------------------

    def test_summaries_split_and_traceable(self):
        outdir = os.path.join(self.tmp.name, "out")
        records_ok = make_record(ip="203.0.113.10", count=5)
        records_fwd = make_record(ip="192.0.2.7", count=3,
                                  spf_result="fail", eval_spf="fail",
                                  spf_domain="mailing.example.net")
        p1 = self._write("a.xml", make_report(rid="r1", records=[records_ok]))
        p2 = self._write("b.xml", make_report(rid="r2", records=[records_fwd]))
        outputs = app.run([p1, p2], self.registry_path, outdir)

        reg = outputs["registered_sources.json"]
        unk = outputs["unknown_sources.json"]
        fwd = outputs["forwarding_risk.json"]
        self.assertEqual(reg["total_count"], 5)
        self.assertEqual(reg["report_ids"], ["ReporterOne:r1"])
        self.assertEqual(unk["total_count"], 3)
        self.assertEqual(unk["report_ids"], ["ReporterOne:r2"])
        self.assertEqual(fwd["total_count"], 3)
        self.assertEqual(fwd["rows"][0]["report_ids"], ["ReporterOne:r2"])
        self.assertTrue(fwd["rows"][0]["relies_on_forwarding"])
        for name in ("registered_sources.json", "unknown_sources.json",
                     "forwarding_risk.json", "review.json"):
            with open(os.path.join(outdir, name), encoding="utf-8") as fh:
                json.load(fh)  # must be valid JSON

    def test_cli_end_to_end(self):
        outdir = os.path.join(self.tmp.name, "out")
        p1 = self._write("a.xml", make_report(records=[make_record(count=4)]))
        rc = app.main(["--registry", self.registry_path,
                       "--outdir", outdir, p1])
        self.assertEqual(rc, 0)
        with open(os.path.join(outdir, "registered_sources.json"),
                  encoding="utf-8") as fh:
            reg = json.load(fh)
        self.assertEqual(reg["total_count"], 4)


if __name__ == "__main__":
    unittest.main()
