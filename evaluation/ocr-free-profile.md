# OCR profile verification — 11 October 2026

The first live image-only/native upload check on revision `e443704` failed during EXTRACTING with an OCR page timeout. No graph/generation provider call was made for that upload. The disposable fixture was removed successfully; existing uploads were not changed. Health and a separate cited-answer query passed, so readiness alone did not establish OCR usability.

## Local constrained check

The current application image was tested with networking disabled, a 0.5-CPU quota and 512 MB memory, using the same authored `equipment-inspection.pdf`. The engine was Tesseract 5.3.0. The local container is not identical to Render hardware or contention conditions; both local profiles passed, so this is not a reproduction of the live timeout.

| Profile | Page deadline | Elapsed | Recovered characters | OCR confidence | Methods |
| --- | ---: | ---: | ---: | ---: | --- |
| 4 OCR threads | 20 s | 2.414 s | 1,250 | 95.6 | OCR + native text |
| 1 OCR thread | 60 s | 1.194 s | 1,250 | 95.6 | OCR + native text |

These are single-fixture observations, not an OCR-accuracy, throughput or production-speed guarantee. No quality threshold was lowered. The parser sets `OMP_THREAD_LIMIT` only in the Tesseract child process from bounded `OCR_THREADS` (1–4, default 1). The parent process environment remains unchanged. Render's free profile uses one thread and a 60-second shared per-page deadline; optional retries still share that deadline and rendering/page caps remain enforced.

Automated checks: 299 backend tests passed, including child-environment isolation/thread bounds; eight frontend status tests passed; 24/24 authored sample pages passed extraction verification. The application Docker image built successfully.

Live verification after redeployment is required before claiming the timeout is resolved on Render.

Primary guidance: [Tesseract FAQ — OCR speed and thread limits](https://tesseract-ocr.github.io/tessdoc/FAQ.html).
