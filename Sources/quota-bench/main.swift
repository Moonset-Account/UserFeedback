//
//  main.swift
//  quota-bench
//
//  用法：
//    quota-bench                 运行内置合成演示场景
//    quota-bench <input.json>    运行本地 JSON 场景
//

import Foundation
import QuotaIsolationCore

let args = CommandLine.arguments

let quotas: [TenantID: Int]
let config: QuotaConfig
let events: [QuotaEvent]
let sourceLabel: String

if args.count >= 2 {
    let path = args[1]
    do {
        let loaded = try ScenarioLoader.load(contentsOf: path)
        quotas = loaded.quotas
        config = loaded.config
        events = loaded.events
        sourceLabel = "file \(path)"
    } catch {
        FileHandle.standardError.write(Data("failed to load scenario: \(error)\n".utf8))
        exit(2)
    }
} else {
    let demo = DemoScenario.make()
    quotas = demo.quotas
    config = demo.config
    events = demo.events
    sourceLabel = "built-in demo scenario"
}

do {
    let engine = try QuotaEngine(quotas: quotas, config: config)
    let ledger = try engine.run(events)
    print("input: \(sourceLabel)")
    print(LedgerPrinter.render(quotas: quotas, config: config, ledger: ledger))
} catch {
    FileHandle.standardError.write(Data("scenario rejected: \(error)\n".utf8))
    exit(1)
}
