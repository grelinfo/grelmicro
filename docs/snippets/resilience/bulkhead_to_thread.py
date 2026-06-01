import time

from grelmicro.resilience import Bulkhead

# A dedicated 4-thread pool keeps blocking work off the event loop and
# off the default executor shared by the rest of the app.
reports = Bulkhead("reports", max_workers=4)


def render_pdf(report_id: str) -> bytes:
    time.sleep(0.1)  # blocking work
    return report_id.encode()


@reports
async def build_report(report_id: str) -> bytes:
    return await reports.to_thread(render_pdf, report_id)
