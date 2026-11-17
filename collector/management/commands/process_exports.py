import time

from django.core.management.base import BaseCommand

from collector.export_jobs import claim_next_export_job, cleanup_export_jobs, process_export_job


class Command(BaseCommand):
    help = "Process queued student export jobs without occupying web workers."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        once = options["once"]
        poll_seconds = max(0.5, options["poll_seconds"])
        while True:
            cleanup_export_jobs()
            job = claim_next_export_job()
            if job is not None:
                self.stdout.write(f"processing export job {job.pk}")
                process_export_job(job)
            elif once:
                return
            else:
                time.sleep(poll_seconds)
