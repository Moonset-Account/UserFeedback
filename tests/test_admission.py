"""容器发布准入检查的单元测试（unittest，纯标准库）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admission  # noqa: E402

NOW = "2026-11-14T00:00:00Z"
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def workload(containers=None, init=None, volumes=None, defaults=None,
             namespace="payments", name="checkout"):
    doc = {"namespace": namespace, "name": name}
    if defaults is not None:
        doc["defaults"] = defaults
    if containers is not None:
        doc["containers"] = containers
    if init is not None:
        doc["initContainers"] = init
    if volumes is not None:
        doc["volumes"] = volumes
    return doc


def container(name="app", image="repo/app@sha256:aaa", privileged=None,
              run_as_user="unset", mounts=None):
    c = {"name": name, "image": image}
    sc = {}
    if privileged is not None:
        sc["privileged"] = privileged
    if run_as_user != "unset":
        sc["runAsUser"] = run_as_user
    if sc:
        c["securityContext"] = sc
    if mounts:
        c["volumeMounts"] = mounts
    return c


def exception(violation, digest="sha256:aaa", container_name="app",
              namespace="payments", workload_name="checkout",
              host_path=None, expires="2027-01-01T00:00:00Z", exc_id="EX-1"):
    e = {"id": exc_id, "namespace": namespace, "workload": workload_name,
         "container": container_name, "imageDigest": digest,
         "violation": violation, "expiresAt": expires}
    if host_path is not None:
        e["hostPath"] = host_path
    return e


def check(candidate, **kwargs):
    kwargs.setdefault("now", NOW)
    return admission.check_release(candidate, **kwargs)


def open_violations(result):
    return [v for v in result["candidate"]["violations"] if not v["suppressed"]]


class TestPrivilegedAndRootAndHostPath(unittest.TestCase):
    def test_regular_and_init_containers_all_checked(self):
        """普通容器和初始化容器都要检查特权、root、宿主机挂载。"""
        candidate = workload(
            containers=[container("app", privileged=True, run_as_user=0,
                                  mounts=[{"name": "data", "mountPath": "/data"}])],
            init=[container("init", image="repo/init@sha256:bbb", privileged=True,
                            run_as_user=0,
                            mounts=[{"name": "data", "mountPath": "/data"}])],
            volumes=[{"name": "data", "hostPath": {"path": "/var/lib/x"}}],
            defaults={"runAsUser": 1000},
        )
        result = check(candidate)
        self.assertEqual(result["decision"], "block")
        by_loc = {}
        for v in result["candidate"]["violations"]:
            by_loc.setdefault(v["location"], set()).add(v["type"])
        self.assertEqual(by_loc["containers[0](app)"],
                         {admission.PRIVILEGED, admission.RUN_AS_ROOT, admission.HOST_PATH})
        self.assertEqual(by_loc["initContainers[0](init)"],
                         {admission.PRIVILEGED, admission.RUN_AS_ROOT, admission.HOST_PATH})

    def test_clean_workload_allowed(self):
        candidate = workload(
            containers=[container("app", privileged=False, run_as_user=1000)],
            init=[container("init", image="repo/init@sha256:bbb", run_as_user=1000)],
        )
        result = check(candidate)
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["candidate"]["violations"], [])

    def test_violation_reports_location_value_and_exception_status(self):
        """逐项指出违规位置、实际生效值和例外匹配情况。"""
        candidate = workload(
            containers=[container("app", privileged=True, run_as_user=1000)],
        )
        result = check(candidate)
        v = result["candidate"]["violations"][0]
        self.assertEqual(v["location"], "containers[0](app)")
        self.assertIs(v["effectiveValue"], True)
        self.assertEqual(v["exception"], {"status": "none", "exceptionId": None})
        self.assertFalse(v["suppressed"])
        self.assertIn("containers[0](app)", result["reasons"][0])


class TestRunAsUserOverride(unittest.TestCase):
    def test_container_uid_overrides_workload_default_to_root(self):
        """容器显式 UID=0 覆盖工作负载默认 1000，仍按 root 拦截。"""
        candidate = workload(containers=[container("app", run_as_user=0)],
                             defaults={"runAsUser": 1000})
        result = check(candidate)
        violations = open_violations(result)
        self.assertEqual([v["type"] for v in violations], [admission.RUN_AS_ROOT])
        self.assertEqual(violations[0]["effectiveValue"], 0)
        self.assertEqual(result["decision"], "block")

    def test_container_uid_overrides_workload_default_to_nonroot(self):
        """容器显式 UID=1000 覆盖工作负载默认 0，实际生效值非 root，不报 root。"""
        candidate = workload(containers=[container("app", run_as_user=1000)],
                             defaults={"runAsUser": 0})
        result = check(candidate)
        self.assertEqual(result["candidate"]["violations"], [])
        self.assertEqual(result["decision"], "allow")

    def test_workload_default_used_when_container_silent(self):
        candidate = workload(containers=[container("app")],
                             defaults={"runAsUser": 0})
        result = check(candidate)
        violations = open_violations(result)
        self.assertEqual([v["type"] for v in violations], [admission.RUN_AS_ROOT])

    def test_unconfirmed_identity_blocks(self):
        """容器与工作负载默认值都没有 runAsUser 时，以运行身份无法确认拦截。"""
        candidate = workload(containers=[container("app")])
        result = check(candidate)
        self.assertEqual(result["decision"], "block")
        violations = open_violations(result)
        self.assertEqual([v["type"] for v in violations],
                         [admission.RUN_IDENTITY_UNCONFIRMED])
        self.assertIsNone(violations[0]["effectiveValue"])
        self.assertIn("运行身份无法确认", violations[0]["message"])


class TestExceptions(unittest.TestCase):
    def candidate(self, digest="sha256:aaa", path="/var/lib/x"):
        return workload(
            containers=[container("app", image="repo/app@" + digest, privileged=True,
                                  run_as_user=1000,
                                  mounts=[{"name": "data", "mountPath": "/data"}])],
            volumes=[{"name": "data", "hostPath": {"path": path}}],
        )

    def test_full_match_suppresses(self):
        exc = exception(admission.PRIVILEGED)
        result = check(self.candidate(), exceptions=[exc])
        priv = [v for v in result["candidate"]["violations"]
                if v["type"] == admission.PRIVILEGED][0]
        self.assertTrue(priv["suppressed"])
        self.assertEqual(priv["exception"], {"status": "applied", "exceptionId": "EX-1"})

    def test_digest_change_invalidates_exception(self):
        """镜像摘要变了，原先批准的例外必须重新核对：摘要不匹配则例外失效。"""
        exc = exception(admission.PRIVILEGED, digest="sha256:old")
        result = check(self.candidate(digest="sha256:new"), exceptions=[exc])
        priv = [v for v in result["candidate"]["violations"]
                if v["type"] == admission.PRIVILEGED][0]
        self.assertFalse(priv["suppressed"])
        self.assertEqual(priv["exception"]["status"], "none")
        self.assertEqual(result["decision"], "block")

    def test_field_mismatch_does_not_suppress(self):
        """命名空间/工作负载/容器/违规项任一不匹配都不生效。"""
        base = self.candidate()
        cases = [
            exception(admission.PRIVILEGED, namespace="other"),
            exception(admission.PRIVILEGED, workload_name="other"),
            exception(admission.PRIVILEGED, container_name="other"),
            exception(admission.RUN_AS_ROOT),
        ]
        for exc in cases:
            result = check(base, exceptions=[exc])
            priv = [v for v in result["candidate"]["violations"]
                    if v["type"] == admission.PRIVILEGED][0]
            self.assertFalse(priv["suppressed"],
                             "例外不应生效: %r" % (exc,))

    def test_expired_exception_does_not_suppress(self):
        exc = exception(admission.PRIVILEGED, expires="2026-01-01T00:00:00Z")
        result = check(self.candidate(), exceptions=[exc])
        priv = [v for v in result["candidate"]["violations"]
                if v["type"] == admission.PRIVILEGED][0]
        self.assertFalse(priv["suppressed"])
        self.assertEqual(priv["exception"], {"status": "expired", "exceptionId": "EX-1"})
        self.assertEqual(result["decision"], "block")

    def test_hostpath_exception_requires_path_match(self):
        """挂载类例外还要匹配宿主机路径。"""
        exc = exception(admission.HOST_PATH, host_path="/var/lib/other")
        result = check(self.candidate(path="/var/lib/x"), exceptions=[exc])
        hp = [v for v in result["candidate"]["violations"]
              if v["type"] == admission.HOST_PATH][0]
        self.assertFalse(hp["suppressed"])

    def test_hostpath_exception_full_match_suppresses(self):
        exc = exception(admission.HOST_PATH, host_path="/var/lib/x")
        result = check(self.candidate(path="/var/lib/x"), exceptions=[exc])
        hp = [v for v in result["candidate"]["violations"]
              if v["type"] == admission.HOST_PATH][0]
        self.assertTrue(hp["suppressed"])
        self.assertEqual(hp["effectiveValue"], "/var/lib/x")

    def test_tag_image_without_digest_cannot_match_exception(self):
        """镜像只有 tag 没有摘要时，例外无法匹配摘要，违规不被豁免。"""
        candidate = workload(
            containers=[container("app", image="repo/app:latest", privileged=True,
                                  run_as_user=1000)])
        exc = exception(admission.PRIVILEGED, digest="sha256:aaa")
        result = check(candidate, exceptions=[exc])
        priv = [v for v in result["candidate"]["violations"]
                if v["type"] == admission.PRIVILEGED][0]
        self.assertIsNone(priv["imageDigest"])
        self.assertFalse(priv["suppressed"])
        self.assertEqual(result["decision"], "block")


class TestBaseline(unittest.TestCase):
    def test_allowed_paths_in_baseline(self):
        candidate = workload(
            containers=[container("app", run_as_user=1000,
                                  mounts=[{"name": "c", "mountPath": "/c"}])],
            volumes=[{"name": "c", "hostPath": {"path": "/var/log/cache/app"}}],
        )
        baseline = {"rules": {admission.HOST_PATH: {"allowedPaths": ["/var/log/cache"]}}}
        result = check(candidate, baseline=baseline)
        self.assertEqual(result["candidate"]["violations"], [])

    def test_rule_can_be_disabled_by_baseline(self):
        candidate = workload(containers=[container("app", privileged=True,
                                                   run_as_user=1000)])
        baseline = {"rules": {admission.PRIVILEGED: {"enforced": False}}}
        result = check(candidate, baseline=baseline)
        self.assertEqual(result["decision"], "allow")


class TestDeployedDiff(unittest.TestCase):
    def test_new_violations_marked_against_deployed(self):
        """用相同基线评估两个版本，标出候选版本新增的违规。"""
        deployed = workload(
            containers=[container("app", image="repo/app@sha256:old", privileged=True,
                                  run_as_user=0)],
        )
        candidate = workload(
            containers=[container("app", image="repo/app@sha256:new", privileged=True,
                                  run_as_user=0,
                                  mounts=[{"name": "d", "mountPath": "/d"}])],
            volumes=[{"name": "d", "hostPath": {"path": "/etc"}}],
        )
        result = check(candidate, deployed=deployed)
        self.assertEqual(result["decision"], "block")
        by_type = {v["type"]: v for v in result["candidate"]["violations"]}
        # 特权与 root 在已上线版本已存在 → 非新增
        self.assertFalse(by_type[admission.PRIVILEGED]["isNew"])
        self.assertFalse(by_type[admission.RUN_AS_ROOT]["isNew"])
        # 宿主机挂载是候选版本新增
        self.assertTrue(by_type[admission.HOST_PATH]["isNew"])
        self.assertEqual(result["summary"]["newViolations"], 1)
        self.assertEqual([v["type"] for v in result["newViolations"]],
                         [admission.HOST_PATH])
        # 已上线版本也用同一基线评估并给出违规
        self.assertEqual(len(result["deployed"]["violations"]), 2)

    def test_no_deployed_means_is_new_none(self):
        candidate = workload(containers=[container("app", privileged=True,
                                                   run_as_user=1000)])
        result = check(candidate)
        self.assertIsNone(result["candidate"]["violations"][0]["isNew"])
        self.assertNotIn("deployed", result)


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(WORKSPACE, "app.py"), *args],
            capture_output=True, text=True, timeout=30)

    def test_cli_block_exit_1_and_allow_exit_0(self):
        blocked = self.run_cli(
            "--candidate", os.path.join(WORKSPACE, "examples", "candidate_blocked.json"),
            "--baseline", os.path.join(WORKSPACE, "examples", "baseline.json"),
            "--exceptions", os.path.join(WORKSPACE, "examples", "exceptions.json"),
            "--now", NOW)
        self.assertEqual(blocked.returncode, 1, blocked.stderr)
        payload = json.loads(blocked.stdout)
        self.assertEqual(payload["decision"], "block")

        allowed = self.run_cli(
            "--candidate", os.path.join(WORKSPACE, "examples", "candidate_allowed.json"),
            "--baseline", os.path.join(WORKSPACE, "examples", "baseline.json"),
            "--now", NOW)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout)["decision"], "allow")

    def test_cli_bad_input_exit_2(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            bad = fh.name
        try:
            proc = self.run_cli("--candidate", bad)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error", proc.stderr)
        finally:
            os.unlink(bad)


if __name__ == "__main__":
    unittest.main()
