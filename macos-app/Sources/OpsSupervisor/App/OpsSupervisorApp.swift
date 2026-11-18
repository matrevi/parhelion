import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct OpsSupervisorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var store = ContestStore()

    var body: some Scene {
        WindowGroup("Parhelion · Operator & NPU Supervisor") {
            AppRouterView()
                .environmentObject(store)
                .frame(minWidth: 1120, minHeight: 720)
        }
        .defaultSize(width: 1340, height: 840)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Refresh All Telemetry") {
                    Task { await store.refreshAll() }
                }
                .keyboardShortcut("r", modifiers: [.command])
            }
        }

        Settings {
            SettingsRootView()
                .environmentObject(store)
                .frame(width: 680, height: 520)
        }
    }
}
