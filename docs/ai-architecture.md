# AI architecture

How the language model is used, what it is allowed to see, and how its answer is checked. The
LLM never selects evidence and never sets severity: it interprets evidence that deterministic
code selected, and everything it claims is verified before it is stored or shown.

## Models and providers

| Role | Default | Alternatives | Code |
|---|---|---|---|
| Diagnosis LLM | `stub` (LOCAL-ONLY, deterministic) | `openai_compatible` (OpenAI, gateways, vLLM, Ollama), `gemini` | `app/ai/llm_clients.py` |
| Embeddings (historical retrieval) | `sentence_transformers` `all-MiniLM-L6-v2` (optional `embeddings` extra) | `hashing` (LOCAL-ONLY, used by Docker, the demo and all reported results), `openai`, `gemini` | `app/rag/embedders.py` |
| Vector store | FAISS (NumPy exact search without faiss) | pgvector | `app/rag/vector_store.py` |

One embedding model per index; model name and dimension are stored with the index and checked
on every add and search (D61). Real-LLM settings and commands: `docs/experiments/RUNBOOK.md`.

**The stub is not a model.** It answers from fixed keyword rules over the evidence lines in
the prompt, always produces schema-valid JSON and only cites lines it was shown. It exists so
that every stage runs and can be tested without keys. Its label agreement on the synthetic
data is not a result (D75): its rules were written against the simulator's wording.

## Prompt

Version `diag-v1` (`app/diagnosis/prompts.py`); the template hash is recorded with every
diagnosis and experiment (D73).

- **System rules:** use only the context; cite evidence ids for every factual claim; label
  each statement FACT, INFERENCE, HYPOTHESIS or RECOMMENDATION; choose `insufficient_evidence`
  when the context does not support a conclusion; historical incidents are context, not
  evidence.
- **Output spec:** the Diagnosis schema (BRIEF Section 2) as JSON.
- **Context** (`app/diagnosis/context.py`, D72), in this order: INCIDENT (blinded
  description, alarm, affected resources), TIMELINE (candidate causes and chains), ANOMALIES,
  LOGS, CLOUDTRAIL (with Config changes), GRAPH (dependency and impact paths), HISTORICAL.
  Each observed fact is one line starting with its `evd_` id. A token budget (6,000 estimated
  tokens) is shared between sections with carry-over; dropped items are counted.
- **Ablations** remove a section or a signal: A1 no HISTORICAL, A2 no GRAPH and dependency
  weight 0, A3 no ANOMALIES and anomaly weight 0, A4 no chains and temporal weight 0, A5
  recency instead of ranking; B2 description only; B3 raw telemetry newest first under the
  same budget; B4 ranked evidence without graph and RAG.

## Redaction (before any external call)

`app/ai/redaction.py` (D59, D67-D69). Every external `LLMClient` and `Embedder` is created by
`create_llm_client` / `create_embedder` and wrapped in a redacting client; direct construction
raises. The wrapper replaces account ids, ARNs, IP addresses, principals (IAM ids, access key
ids, e-mails), hostnames and secrets (key=value, bearer tokens, JWTs, private keys, URL
passwords, bare 40-character keys, custom patterns) with consistent pseudonyms (`ACCOUNT_1`,
`IP_2`, ...), keeps one mapping per incident across the primary answer, repair and samples,
and maps pseudonyms in the answer back (never secrets). Strict mode refuses to send text that
still contains a value it replaced. In aws mode the full policy is mandatory and the factory
fails closed (D68). Canonical resource ids stay readable so citations can be checked; set
`redaction.resource_names: true` to pseudonymise them too.

Redaction is pattern-based: identifiers in formats it does not recognise can still reach the
provider (`docs/known-limitations.md`).

## Historical retrieval (RAG)

- **Corpus:** `historical-v1`, 40 past incidents from the simulator with a different seed and
  different scenario keys; never test incidents (D62). A leakage guard compares every record's
  incident id, event ids and description hash with the test split and refuses the whole build.
- **Query:** the blinded description plus the top-K evidence and candidate-cause rationales,
  i.e. only what is known at diagnosis time (D63).
- **Ranking:** 20 nearest neighbours, re-ranked by 0.7 cosine + 0.2 signal overlap + 0.1
  context; top 3 enter the prompt as HISTORICAL, labelled "context only, NOT evidence".
- **Use is declared and checked:** the answer's `historical_influence` must name the past
  incidents it used and how; citing a historical id as evidence, declaring an id that was not
  retrieved, or copying a past root cause verbatim fails validation (D64).

## Validation, repair, rejection

`app/diagnosis/validator.py` (D74):

1. Parse JSON (code fences tolerated); validate the Pydantic `Diagnosis` schema (taxonomy is a
   closed set; root cause plus alternatives confidences sum to at most 1; alternatives ranked).
2. **Citation verifier:** every cited `evidence_id` must be in the context; numbers quoted in
   FACT explanations must match the cited line (relative tolerance 1%); the root-cause resource
   must be a known resource; at least one supporting citation for any conclusion.
3. Historical-influence rules (above).
4. On failure: exactly one repair request (the errors are sent back, temperature 0). Still
   invalid: the diagnosis is stored as rejected with its reasons and never shown as a
   conclusion.
5. **Policy:** `requires_human_review` is forced on below the confidence threshold (0.6) and
   for `insufficient_evidence`.

The verifier is lexical. An audit with injected faults (`docs/experiments/phase7-verifier-audit.md`)
measures what it catches; false claims that reuse words from the cited line can pass.

## Confidence

- **Verbalised:** the model's stated `root_cause.confidence`.
- **Self-consistency:** N extra samples at temperature 0.7; confidence = share of samples
  whose label equals the primary answer's, invalid samples counted as disagreeing (D77).
- Calibration (ECE, Brier) is reported for both in every experiment, with the small-sample
  caveat (`docs/evaluation.md`).

## Severity

`app/severity/engine.py` (D76): points for affected resources/services, peak error rate,
duration, capacity loss and latency ratio, plus configured business criticality, mapped to
LOW/MEDIUM/HIGH/CRITICAL. The incident's severity is always this engine's; the model's
`severity_suggestion` is stored and shown as advisory only.

## Where the model's output goes

Diagnoses are stored with the prompt hash, model id reported by the provider, tokens, latency
and cost estimate. The API returns them with the event behind every citation; the UI applies
the evidence rule (D102) before showing a root cause. Nothing the model writes triggers an
action: recommendations are text for a human.
