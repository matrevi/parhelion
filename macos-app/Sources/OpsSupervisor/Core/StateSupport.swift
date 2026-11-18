import SwiftUI

/// CommandLineTools-compatible DynamicProperty state wrapper.
@MainActor
final class StateBox<Value>: ObservableObject {
    @Published var value: Value
    init(_ value: Value) { self.value = value }
}

@MainActor
@propertyWrapper
struct UIState<Value>: DynamicProperty {
    @StateObject private var box: StateBox<Value>

    init(wrappedValue: Value) {
        _box = StateObject(wrappedValue: StateBox(wrappedValue))
    }

    var wrappedValue: Value {
        get { box.value }
        nonmutating set { box.value = newValue }
    }

    var projectedValue: Binding<Value> {
        Binding(get: { box.value }, set: { box.value = $0 })
    }
}

/// Epoch-anchored 0.5s wall-clock ticker ensuring zero drift between server timestamps and UI timers.
@MainActor
final class WallClockTicker: ObservableObject {
    @Published var nowEpoch: Double = Date().timeIntervalSince1970
    private var timer: Timer?

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.nowEpoch = Date().timeIntervalSince1970
            }
        }
    }

    func cooldownRemaining(endsAt: Double?) -> Int {
        guard let endsAt, endsAt > 0 else { return 0 }
        return max(0, Int(ceil(endsAt - nowEpoch)))
    }

    func elapsedSince(startedAt: Double?) -> Int {
        guard let startedAt, startedAt > 0 else { return 0 }
        return max(0, Int(floor(nowEpoch - startedAt)))
    }
}
