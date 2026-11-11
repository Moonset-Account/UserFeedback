import XCTest
@testable import ReleaseGateCore

final class ReleaseGateEvaluatorTests: XCTestCase {

    // MARK: - 测试夹具

    private let fixedNow = isoDate("2026-11-12T00:00:00Z")

    private static func isoDate(_ raw: String) -> Date {
        ISO8601DateFormatter().date(from: raw)!
    }

    private static func day(_ raw: String) -> Date {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: raw)!
    }

    private func component(
        _ name: String, _ version: String,
        target: String? = nil, team: String = "platform-team", abi: Int = 3
    ) -> Component {
        Component(
            id: ComponentID(name: name, version: version),
            buildTarget: target ?? "//\(name):\(name)-\(version)",
            team: team,
            abiVersion: abi
        )
    }

    private func edge(_ s: (String, String), _ t: (String, String), _ interface: String) -> IndirectCallEdge {
        IndirectCallEdge(
            source: ComponentID(name: s.0, version: s.1),
            target: ComponentID(name: t.0, version: t.1),
            interface: interface
        )
    }

    private func evaluator() -> ReleaseGateEvaluator {
        ReleaseGateEvaluator(now: fixedNow)
    }

    private func violations(of kind: ViolationKind, in report: GateReport) -> [Violation] {
        report.violations.filter { $0.kind == kind }
    }

    // MARK: - 场景 1：同名插件的不同版本是不同身份

    func testSameNamedPluginDifferentVersions() {
        let manifest = BuildManifest(
            components: [
                component("app", "1.0", target: "//app:app", team: "core-team"),
                component("auth", "2.1", target: "//plugins/auth:auth-2_1", team: "identity-team"),
                component("auth", "3.0", target: "//plugins/auth:auth-3_0", team: "identity-team"),
            ],
            edges: [
                // v2.1 有完整证据
                edge(("app", "1.0"), ("auth", "2.1"), "AuthV2.login"),
                // v3.0 同接口名但新版本缺少声明 -> missing_declaration
                edge(("app", "1.0"), ("auth", "3.0"), "AuthV2.login"),
                // 引用了未纳入清单的 v1.9 -> unknown_component
                edge(("app", "1.0"), ("auth", "1.9"), "AuthV2.login"),
            ],
            coverageClaims: [
                CoverageClaim(
                    component: ComponentID(name: "auth", version: "2.1"),
                    coveredInterfaces: ["AuthV2.login"]
                ),
            ],
            exemptions: []
        )

        let report = evaluator().evaluate(manifest)

        XCTAssertFalse(report.passed)
        XCTAssertEqual(report.summary.evidencedCount, 1)
        let evidenced = report.evidencedCalls.first
        XCTAssertEqual(evidenced?.edge.target, ComponentID(name: "auth", version: "2.1"))
        XCTAssertEqual(evidenced?.buildTarget, "//plugins/auth:auth-2_1")
        XCTAssertEqual(evidenced?.team, "identity-team")

        let missing = violations(of: .missingDeclaration, in: report)
        XCTAssertEqual(missing.count, 1)
        XCTAssertEqual(missing.first?.target, ComponentID(name: "auth", version: "3.0"))
        XCTAssertEqual(missing.first?.buildTarget, "//plugins/auth:auth-3_0")
        XCTAssertEqual(missing.first?.interface, "AuthV2.login")
        XCTAssertEqual(missing.first?.team, "identity-team")

        let unknown = violations(of: .unknownComponent, in: report)
        XCTAssertEqual(unknown.count, 1)
        XCTAssertEqual(unknown.first?.target, ComponentID(name: "auth", version: "1.9"))

        // v2.1 的声明不能让 v3.0 搭便车
        XCTAssertFalse(
            report.evidencedCalls.contains { $0.edge.target == ComponentID(name: "auth", version: "3.0") }
        )
    }

    // MARK: - 场景 2：同一组件同时存在受保护与未覆盖的调用边

    func testMixedCoveredAndUncoveredEdgesOnSameComponent() {
        let manifest = BuildManifest(
            components: [
                component("app", "1.0", target: "//app:app", team: "core-team"),
                component("billing", "4.2", target: "//plugins/billing:billing", team: "payments-team"),
            ],
            edges: [
                edge(("app", "1.0"), ("billing", "4.2"), "Billing.charge"),   // 受保护
                edge(("app", "1.0"), ("billing", "4.2"), "Billing.refund"),   // 未覆盖
            ],
            coverageClaims: [
                CoverageClaim(
                    component: ComponentID(name: "billing", version: "4.2"),
                    coveredInterfaces: ["Billing.charge"]
                ),
            ],
            exemptions: []
        )

        let report = evaluator().evaluate(manifest)

        XCTAssertFalse(report.passed)
        XCTAssertEqual(violations(of: .uncoveredEdge, in: report).count, 1)
        let violation = violations(of: .uncoveredEdge, in: report).first
        XCTAssertEqual(violation?.buildTarget, "//plugins/billing:billing")
        XCTAssertEqual(violation?.interface, "Billing.refund")
        XCTAssertEqual(violation?.team, "payments-team")
        XCTAssertEqual(violation?.sourceBuildTarget, "//app:app")
        XCTAssertEqual(violation?.sourceTeam, "core-team")

        // 受保护的那条边仍被识别为有完整证据
        XCTAssertEqual(report.summary.evidencedCount, 1)
        XCTAssertEqual(report.evidencedCalls.first?.interface, "Billing.charge")
    }

    // MARK: - 场景 3：有效豁免只匹配指定接口（及指定组件身份）

    func testExemptionMatchesOnlyDesignatedInterface() {
        let manifest = BuildManifest(
            components: [
                component("app", "1.0"),
                component("cache", "1.5", target: "//plugins/cache:cache", team: "infra-team"),
            ],
            edges: [
                edge(("app", "1.0"), ("cache", "1.5"), "Cache.flush"),    // 豁免覆盖此接口
                edge(("app", "1.0"), ("cache", "1.5"), "Cache.snapshot"), // 豁免不覆盖 -> uncovered
            ],
            coverageClaims: [], // 整个组件无声明，豁免是其唯一放行依据
            exemptions: [
                Exemption(
                    id: "EX-2026-001",
                    component: ComponentID(name: "cache", version: "1.5"),
                    interface: "Cache.flush",
                    expiresAt: Self.day("2026-12-31")
                )
            ]
        )

        let report = evaluator().evaluate(manifest)

        XCTAssertFalse(report.passed)
        XCTAssertEqual(report.exemptedCalls.count, 1)
        let exempted = report.exemptedCalls.first
        XCTAssertEqual(exempted?.exemptionId, "EX-2026-001")
        XCTAssertEqual(exempted?.interface, "Cache.flush")
        XCTAssertEqual(exempted?.buildTarget, "//plugins/cache:cache")
        XCTAssertEqual(exempted?.team, "infra-team")

        let uncovered = violations(of: .uncoveredEdge, in: report)
        // 无声明时应归类为 missing_declaration 而非 uncovered_edge
        XCTAssertTrue(violations(of: .uncoveredEdge, in: report).isEmpty)
        let missing = violations(of: .missingDeclaration, in: report)
        XCTAssertEqual(missing.count, 1)
        XCTAssertEqual(missing.first?.interface, "Cache.snapshot")
    }

    func testExemptionScopedToComponentIdentity() {
        // 豁免属于 cache 1.5，对 cache 2.0 无效
        let manifest = BuildManifest(
            components: [
                component("app", "1.0"),
                component("cache", "1.5"),
                component("cache", "2.0"),
            ],
            edges: [
                edge(("app", "1.0"), ("cache", "1.5"), "Cache.flush"),
                edge(("app", "1.0"), ("cache", "2.0"), "Cache.flush"),
            ],
            coverageClaims: [],
            exemptions: [
                Exemption(
                    id: "EX-2026-002",
                    component: ComponentID(name: "cache", version: "1.5"),
                    interface: "Cache.flush",
                    expiresAt: Self.day("2026-12-31")
                )
            ]
        )

        let report = evaluator().evaluate(manifest)

        XCTAssertEqual(report.exemptedCalls.count, 1)
        XCTAssertEqual(report.exemptedCalls.first?.edge.target.version, "1.5")
        let missing = violations(of: .missingDeclaration, in: report)
        XCTAssertEqual(missing.count, 1)
        XCTAssertEqual(missing.first?.target?.version, "2.0")
    }

    func testExpiredExemptionIsViolationAndDoesNotPassGate() {
        let manifest = BuildManifest(
            components: [
                component("app", "1.0"),
                component("cache", "1.5", target: "//plugins/cache:cache", team: "infra-team"),
            ],
            edges: [
                edge(("app", "1.0"), ("cache", "1.5"), "Cache.flush"),
            ],
            coverageClaims: [],
            exemptions: [
                Exemption(
                    id: "EX-2025-OLD",
                    component: ComponentID(name: "cache", version: "1.5"),
                    interface: "Cache.flush",
                    expiresAt: Self.day("2026-10-01") // 早于 now=2026-11-12
                )
            ]
        )

        let report = evaluator().evaluate(manifest)

        let expired = violations(of: .expiredExemption, in: report)
        XCTAssertEqual(expired.count, 1)
        XCTAssertEqual(expired.first?.exemptionId, "EX-2025-OLD")
        XCTAssertEqual(expired.first?.buildTarget, "//plugins/cache:cache")
        XCTAssertEqual(expired.first?.interface, "Cache.flush")
        XCTAssertEqual(expired.first?.team, "infra-team")
        XCTAssertTrue(report.exemptedCalls.isEmpty)
        XCTAssertFalse(report.passed)
    }

    // MARK: - 场景 4：依赖图含环

    func testDependencyCycleIsReportedWithComponentRotationDedup() {
        let manifest = BuildManifest(
            components: [
                component("alpha", "1.0", target: "//plugins/alpha:alpha", team: "team-a"),
                component("beta", "1.0", target: "//plugins/beta:beta", team: "team-b"),
                component("gamma", "1.0", target: "//plugins/gamma:gamma", team: "team-c"),
                component("delta", "1.0", target: "//plugins/delta:delta", team: "team-d"),
            ],
            edges: [
                // 环 alpha -> beta -> gamma -> alpha，每条边本身都有完整证据
                edge(("alpha", "1.0"), ("beta", "1.0"), "Beta.api"),
                edge(("beta", "1.0"), ("gamma", "1.0"), "Gamma.api"),
                edge(("gamma", "1.0"), ("alpha", "1.0"), "Alpha.api"),
                // 环外的 DAG 边，不应产生环违规
                edge(("delta", "1.0"), ("alpha", "1.0"), "Alpha.api"),
            ],
            coverageClaims: [
                CoverageClaim(component: ComponentID(name: "beta", version: "1.0"), coveredInterfaces: ["Beta.api"]),
                CoverageClaim(component: ComponentID(name: "gamma", version: "1.0"), coveredInterfaces: ["Gamma.api"]),
                CoverageClaim(component: ComponentID(name: "alpha", version: "1.0"), coveredInterfaces: ["Alpha.api"]),
            ],
            exemptions: []
        )

        let report = evaluator().evaluate(manifest)

        let cycles = violations(of: .dependencyCycle, in: report)
        XCTAssertEqual(cycles.count, 1, "同一环无论从哪个 DFS 入口进入都只报一次")
        let cycle = cycles[0]
        XCTAssertEqual(
            cycle.cycle,
            [
                ComponentID(name: "alpha", version: "1.0"),
                ComponentID(name: "beta", version: "1.0"),
                ComponentID(name: "gamma", version: "1.0"),
            ]
        )
        XCTAssertEqual(
            cycle.cycleBuildTargets,
            ["//plugins/alpha:alpha", "//plugins/beta:beta", "//plugins/gamma:gamma"]
        )
        // 调用边仍按完整证据识别，环是额外的独立违规
        XCTAssertEqual(report.summary.evidencedCount, 4)
        XCTAssertFalse(report.passed)
    }

    func testSelfLoopAndAcyclicGraphDoNotReportCycle() {
        let manifest = BuildManifest(
            components: [
                component("alpha", "1.0"),
                component("beta", "1.0"),
            ],
            edges: [
                // 自环在门禁中按组件内部调用处理，不参与跨组件判定与环检测
                edge(("alpha", "1.0"), ("alpha", "1.0"), "Alpha.internal"),
                edge(("alpha", "1.0"), ("beta", "1.0"), "Beta.api"),
            ],
            coverageClaims: [
                CoverageClaim(component: ComponentID(name: "beta", version: "1.0"), coveredInterfaces: ["Beta.api"]),
            ],
            exemptions: []
        )

        let report = evaluator().evaluate(manifest)

        XCTAssertTrue(violations(of: .dependencyCycle, in: report).isEmpty)
        XCTAssertEqual(report.summary.crossComponentEdgeCount, 1)
        XCTAssertEqual(report.summary.evidencedCount, 1)
        XCTAssertTrue(report.passed)
    }

    // MARK: - ABI 不一致（豁免不可压制）

    func testABIMismatchIsHardViolationEvenWithExemption() {
        let manifest = BuildManifest(
            components: [
                component("app", "1.0", abi: 4),
                component("legacy", "0.9", target: "//plugins/legacy:legacy", team: "legacy-team", abi: 3),
            ],
            edges: [
                edge(("app", "1.0"), ("legacy", "0.9"), "Legacy.hook"),
            ],
            coverageClaims: [],
            exemptions: [
                Exemption(
                    id: "EX-ABI",
                    component: ComponentID(name: "legacy", version: "0.9"),
                    interface: "Legacy.hook",
                    expiresAt: Self.day("2026-12-31")
                )
            ]
        )

        let report = evaluator().evaluate(manifest)

        let mismatch = violations(of: .abiMismatch, in: report)
        XCTAssertEqual(mismatch.count, 1)
        XCTAssertEqual(mismatch.first?.buildTarget, "//plugins/legacy:legacy")
        XCTAssertEqual(mismatch.first?.interface, "Legacy.hook")
        XCTAssertEqual(mismatch.first?.team, "legacy-team")
        XCTAssertTrue(report.exemptedCalls.isEmpty, "ABI 不一致不能被豁免压制")
        XCTAssertFalse(report.passed)
    }

    // MARK: - 全部通过的清单

    func testCleanManifestPasses() {
        let manifest = BuildManifest(
            components: [
                component("app", "1.0", target: "//app:app", team: "core-team"),
                component("billing", "4.2", target: "//plugins/billing:billing", team: "payments-team"),
            ],
            edges: [
                edge(("app", "1.0"), ("billing", "4.2"), "Billing.charge"),
            ],
            coverageClaims: [
                CoverageClaim(component: ComponentID(name: "billing", version: "4.2"), coveredInterfaces: ["Billing.charge"]),
            ],
            exemptions: []
        )

        let report = evaluator().evaluate(manifest)

        XCTAssertTrue(report.passed)
        XCTAssertTrue(report.violations.isEmpty)
        XCTAssertEqual(report.evidencedCalls.first?.team, "payments-team")
    }

    // MARK: - 清单解码：日期与完整往返

    func testManifestDecodingWithDayOnlyAndISO8601Dates() throws {
        let json = """
        {
          "components": [
            {"id": {"name": "app", "version": "1.0"}, "buildTarget": "//app:app", "team": "core", "abiVersion": 3}
          ],
          "edges": [],
          "coverageClaims": [],
          "exemptions": [
            {"id": "E1", "component": {"name": "app", "version": "1.0"}, "interface": "X", "expiresAt": "2026-12-31"},
            {"id": "E2", "component": {"name": "app", "version": "1.0"}, "interface": "Y", "expiresAt": "2026-12-31T23:59:59Z"}
          ]
        }
        """.data(using: .utf8)!

        let manifest = try ManifestLoader.makeDecoder().decode(BuildManifest.self, from: json)
        XCTAssertEqual(manifest.exemptions.first?.expiresAt, Self.day("2026-12-31"))
        XCTAssertEqual(manifest.exemptions[1].expiresAt, Self.isoDate("2026-12-31T23:59:59Z"))

        // 编码往返
        let encoded = try ManifestLoader.makeEncoder(pretty: false).encode(manifest)
        let roundTrip = try ManifestLoader.makeDecoder().decode(BuildManifest.self, from: encoded)
        XCTAssertEqual(roundTrip, manifest)
    }

    func testExemptionExpiresAtBoundaryIsExclusive() {
        // 豁免期限 == 评估时刻即视为过期
        let exemption = Exemption(
            id: "E",
            component: ComponentID(name: "cache", version: "1.5"),
            interface: "Cache.flush",
            expiresAt: fixedNow
        )
        XCTAssertTrue(exemption.isExpired(now: fixedNow))
    }
}
