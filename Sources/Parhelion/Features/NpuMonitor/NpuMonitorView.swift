import SwiftUI

struct NpuMonitorView: View {
    @EnvironmentObject var store: ContestStore

    var body: some View {
        if let snap = store.snapshot {
            VStack(spacing: 0) {
                BtopTelemetryDeckView(npu: snap.npu)
                Divider()
                SchedulerBarView(scheduler: snap.scheduler)
                Divider()

                HSplitView {
                    NpuServerQueueView(npu: snap.npu)
                        .frame(minWidth: 430, idealWidth: 530)

                    SubmissionQueueView(items: snap.submissionQueue)
                        .frame(minWidth: 500, idealWidth: 650)
                }
            }
        }
    }
}
