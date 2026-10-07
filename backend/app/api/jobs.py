"""Asynchronous diagnosis jobs.

`POST /api/incidents/{id}/diagnose` stores a PENDING job and hands it to a thread pool
(`api.job_workers`). The worker:
  1. marks the job RUNNING and moves the incident DETECTED -> INVESTIGATING (actor "system");
  2. loads the incident's stored events and inventory and runs the Phase 4-6 pipeline
     (investigation, retrieval, context, LLM with validation/repair, self-consistency,
     deterministic severity);
  3. stores anomalies, ranked evidence and the diagnosis, sets the incident's severity and
     onset, and moves INVESTIGATING -> DIAGNOSED;
  4. marks the job SUCCEEDED, or FAILED with a scrubbed one-line error (no stack trace).
Jobs left PENDING/RUNNING by a restart are marked FAILED ("interrupted") at start-up. The LLM
client and knowledge base are built once per app; when the knowledge base cannot be built
(e.g. the embedder is not installed) diagnoses run without retrieval and say so (D97).
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.llm_clients import build_llm_client
from app.api import lifecycle, service
from app.config import Settings
from app.db.models import DiagnosisJob
from app.diagnosis.engine import DiagnosisEngine
from app.experiments.config import CONDITIONS
from app.logging_config import scrub
from app.offline.dataset import DatasetLoader
from app.rag.corpus import build_corpus, heldout_fingerprints
from app.rag.embedders import build_embedder
from app.rag.knowledge_base import HistoricalRetriever, KnowledgeBase
from app.rag.vector_store import LocalVectorStore

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[3]


def dataset_dir(settings: Settings) -> Path:
    return Path(settings.api.dataset_dir or REPO / "data" / "generated" / "synthetic-v1")


def corpus_dir(settings: Settings) -> Path:
    return Path(settings.api.corpus_dir or REPO / "data" / "generated" / "historical-v1")


class JobRunner:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings):
        self.sessions = sessions
        self.settings = settings
        self.pool = ThreadPoolExecutor(
            max_workers=settings.api.job_workers, thread_name_prefix="diagnosis"
        )
        self._engine: DiagnosisEngine | None = None
        self._rag_note = ""
        self._lock = threading.Lock()
        self.futures: dict[str, Future] = {}

    # ------------------------------------------------------------------ engine
    def engine(self) -> DiagnosisEngine:
        with self._lock:
            if self._engine is None:
                retriever = None
                if self.settings.rag.enabled:
                    try:
                        loader = DatasetLoader(dataset_dir(self.settings))
                        forbidden = heldout_fingerprints(loader)
                    except Exception:  # noqa: BLE001 - no dataset: nothing held out to guard
                        forbidden = set()
                    try:
                        kb = KnowledgeBase(
                            build_embedder(self.settings),
                            LocalVectorStore(),
                            self.settings.rag.index_name,
                            forbidden,
                        )
                        kb.build(
                            build_corpus(
                                corpus_dir(self.settings),
                                self.settings.rag.corpus_seed,
                                self.settings.rag.corpus_version,
                            )
                        )
                        retriever = HistoricalRetriever(kb, self.settings.rag)
                    except Exception as e:  # noqa: BLE001
                        self._rag_note = f"historical retrieval unavailable: {type(e).__name__}"
                        log.warning(scrub(self._rag_note + f": {e}"))
                self._engine = DiagnosisEngine(
                    build_llm_client(self.settings), self.settings, retriever
                )
            return self._engine

    # ------------------------------------------------------------------ lifecycle of jobs
    def recover(self) -> int:
        with self.sessions() as s:
            stale = s.scalars(
                select(DiagnosisJob).where(DiagnosisJob.status.in_(["PENDING", "RUNNING"]))
            ).all()
            for job in stale:
                job.status, job.error = "FAILED", "interrupted by a restart"
                job.finished_at = lifecycle.now()
            s.commit()
            return len(stale)

    def submit(self, incident_id: str, params: dict, actor: str) -> DiagnosisJob:
        with self.sessions() as s:
            service.get_incident(s, incident_id)
            job = DiagnosisJob(
                id="job-" + uuid.uuid4().hex[:16],
                incident_id=incident_id,
                status="PENDING",
                params=params,
                created_at=lifecycle.now(),
                requested_by=actor,
            )
            s.add(job)
            s.commit()
            s.refresh(job)
        self.futures[job.id] = self.pool.submit(self._run, job.id)
        return job

    def _set(self, job_id: str, **fields) -> None:
        with self.sessions() as s:
            job = s.get(DiagnosisJob, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            s.commit()

    def _run(self, job_id: str) -> None:
        try:
            with self.sessions() as s:
                job = s.get(DiagnosisJob, job_id)
                job.status, job.started_at = "RUNNING", lifecycle.now()
                inc = service.get_incident(s, job.incident_id)
                if inc.status == lifecycle.Status.DETECTED.value:
                    lifecycle.transition(
                        s,
                        inc,
                        lifecycle.Status.INVESTIGATING,
                        "system",
                        f"diagnosis job {job_id} started",
                    )
                params, incident_id = dict(job.params), job.incident_id
                s.commit()
            with self.sessions() as s:
                _, record, events, resources, relationships = service.load_case(s, incident_id)
            if not events:
                raise ValueError("no telemetry stored for this incident: ingest events first")
            cond = CONDITIONS[params.get("condition", "Full")]
            engine = self.engine()
            inv = engine.pipeline.run(
                incident=record,
                events=events,
                resources=resources,
                relationships=relationships,
                use_graph=cond.use_graph,
                use_anomalies=cond.use_anomalies,
                use_chains=cond.use_chains,
                ranking_mode=cond.ranking_mode,
                weights=cond.weights(self.settings.evidence.weights),
            )
            result = engine.diagnose(
                incident=record,
                events=events,
                resources=resources,
                relationships=relationships,
                condition=cond.name,
                use_graph=cond.use_graph,
                use_anomalies=cond.use_anomalies,
                use_chains=cond.use_chains,
                use_rag=cond.use_rag and engine.retriever is not None,
                samples=params.get("samples"),
                investigation=inv,
            )
            with self.sessions() as s:
                diag_id = service.persist_result(
                    s,
                    incident_id,
                    result,
                    inv,
                    {"job_id": job_id, "rag_note": self._rag_note or None},
                )
                inc = service.get_incident(s, incident_id)
                if result.valid and inc.status == lifecycle.Status.INVESTIGATING.value:
                    lifecycle.transition(
                        s,
                        inc,
                        lifecycle.Status.DIAGNOSED,
                        "system",
                        f"diagnosis {diag_id} ({result.model})",
                    )
                job = s.get(DiagnosisJob, job_id)
                job.status = "SUCCEEDED" if result.valid else "FAILED"
                job.error = (
                    None if result.valid else "the model's answer was rejected by the validator"
                )
                job.diagnosis_id, job.finished_at = diag_id, lifecycle.now()
                s.commit()
        except Exception as e:  # noqa: BLE001 - a job must always end in a terminal state
            log.warning("diagnosis job %s failed: %s", job_id, scrub(f"{type(e).__name__}: {e}"))
            self._set(
                job_id,
                status="FAILED",
                finished_at=lifecycle.now(),
                error=scrub(f"{type(e).__name__}: {e}")[:500],
            )

    def wait(self, job_id: str, timeout: float = 60) -> None:
        f = self.futures.get(job_id)
        if f is not None:
            f.result(timeout=timeout)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=False)
