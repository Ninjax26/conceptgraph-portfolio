# OCR extraction check

Authored English fixtures; not a real-document or retrieval-quality benchmark.

Engine: tesseract 5.5.2.

CER/WER are edit-distance error rates (lower is better). Text is casefolded and whitespace normalized; punctuation is retained.

| Fixture | Baseline CER | Current CER | Baseline WER | Current WER | Baseline seconds | Current seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| native | 0.00% | 0.00% | 0.00% | 0.00% | 0.005 | 0.004 |
| clean_scan | 0.00% | 0.00% | 0.00% | 0.00% | 0.548 | 0.493 |
| rotated_90_scan | 0.00% | 0.00% | 0.00% | 0.00% | 0.494 | 0.492 |
| rotated_180_scan | 84.41% | 0.00% | 100.00% | 0.00% | 0.51 | 0.491 |
| low_resolution_scan | 0.00% | 0.00% | 0.00% | 0.00% | 0.437 | 0.493 |
| slightly_skewed_scan | 0.00% | 0.00% | 0.00% | 0.00% | 0.516 | 0.515 |
| mixed_native_image | 88.37% | 0.00% | 89.92% | 0.00% | 0.004 | 0.518 |
| corrupted_hidden_text | 99.73% | 0.00% | 100.00% | 0.00% | 0.005 | 0.568 |

These results do not establish OCR accuracy on Japanese documents, handwriting, tables, CAD drawings, or production scans.
Recognition confidence and the text corruption score are diagnostics, not calibrated accuracy probabilities.
