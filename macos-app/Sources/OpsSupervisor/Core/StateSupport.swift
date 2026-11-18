import SwiftUI

/// CommandLineTools-compatible DynamicProperty state wrapper (avoids missing libSwiftUIMacros.dylib).
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

/// Scoped 1-second countdown ticker isolated from root store to avoid global UI invalidation.
@MainActor
final class CooldownTicker: ObservableObject {
    @Published var remainingSec: Int = 0
    private var timer: Timer?
    var onZeroReached: (() -> Void)?

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                if self.remainingSec > 0 {
                    self.remainingSec -= 1
                    if self.remainingSec == 0 {
                        self.onZeroReached?()
                    }
                }
            }
        }
    }

    func sync(_ sec: Int) {
        if abs(remainingSec - sec) > 2 || sec == 0 {
            remainingSec = max(0, sec)
        }
    }
}
