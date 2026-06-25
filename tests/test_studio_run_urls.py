"""Studio admin review URLs returned to Mise."""

from app import config, service


def test_studio_run_urls_run(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://argus.test")
    assert service.studio_run_urls(run_id=9) == {"review_url": "http://argus.test/runs/9"}


def test_studio_run_urls_job(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://argus.test")
    assert service.studio_run_urls(job_id="job-1") == {
        "review_url": "http://argus.test/ui/jobs/job-1"
    }