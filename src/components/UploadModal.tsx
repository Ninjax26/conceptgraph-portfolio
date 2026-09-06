import { FormEvent, useRef, useState } from "react";
import { FileUp, Loader2, X } from "lucide-react";
import { IngestResponse, uploadDocument } from "../services/api";

interface UploadModalProps {
  isOpen: boolean;
  onClose: () => void;
  onUploaded?: (upload: IngestResponse) => void;
}

const MAX_FILE_BYTES = 10 * 1024 * 1024;

export default function UploadModal({
  isOpen,
  onClose,
  onUploaded,
}: UploadModalProps): JSX.Element | null {
  const [courseId, setCourseId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const canSubmitUpload = file !== null && courseId.trim().length > 0;

  if (!isOpen) return null;

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files?.[0];
    if (selected && selected.size > MAX_FILE_BYTES) {
      setMessage({ text: "PDF files must be 10 MB or smaller.", type: "error" });
      setFile(null);
    } else if (selected && selected.type === "application/pdf") {
      setFile(selected);
      setMessage(null);
    } else if (selected) {
      setMessage({ text: "Please select a valid PDF file.", type: "error" });
      setFile(null);
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const dropped = e.dataTransfer.files?.[0];
    if (dropped && dropped.size > MAX_FILE_BYTES) {
      setMessage({ text: "PDF files must be 10 MB or smaller.", type: "error" });
      setFile(null);
    } else if (dropped && dropped.type === "application/pdf") {
      setFile(dropped);
      setMessage(null);
    } else if (dropped) {
      setMessage({ text: "Please drop a valid PDF file.", type: "error" });
    }
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmitUpload || file === null) return;

    setIsUploading(true);
    setMessage(null);

    try {
      const res = await uploadDocument(file, courseId.trim());
      onUploaded?.(res);
      setMessage({ text: res.message || "Background processing has started.", type: "success" });
      setTimeout(() => {
        onClose();
        setFile(null);
        setCourseId("");
        setMessage(null);
      }, 2000);
    } catch (err) {
      setMessage({
        text: err instanceof Error ? err.message : "Failed to upload document",
        type: "error",
      });
    } finally {
      setIsUploading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-slate-800">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-900 dark:text-white">Upload Syllabus</h2>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-500 dark:hover:bg-slate-700"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <p className="rounded-md bg-amber-50 p-3 text-xs leading-5 text-amber-900">Uploads are shared with all reviewers. Documents added to the configured public sample course are visible to public visitors. Use non-confidential material only.</p>
          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
              Course ID
            </label>
            <input
              type="text"
              value={courseId}
              onChange={(e) => setCourseId(e.target.value)}
              placeholder="e.g., machine-learning-101"
              className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 shadow-sm focus:border-teal-500 focus:outline-none focus:ring-1 focus:ring-teal-500 dark:border-slate-600 dark:bg-slate-700 dark:text-white"
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300">
              Document (PDF)
            </label>
            <div
              className={`mt-1 flex justify-center rounded-md border-2 border-dashed px-6 pt-5 pb-6 ${
                file ? "border-teal-500 bg-teal-50 dark:bg-teal-900/20" : "border-slate-300 dark:border-slate-600"
              }`}
              onDragOver={(e) => e.preventDefault()}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <div className="space-y-1 text-center cursor-pointer">
                <FileUp className="mx-auto h-12 w-12 text-slate-400" />
                <div className="flex text-sm text-slate-600 dark:text-slate-400">
                  <span className="relative cursor-pointer rounded-md font-medium text-teal-600 focus-within:outline-none focus-within:ring-2 focus-within:ring-teal-500 focus-within:ring-offset-2 hover:text-teal-500">
                    {file ? file.name : "Upload a file"}
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,application/pdf"
                      className="sr-only"
                      onChange={handleFileChange}
                    />
                  </span>
                  {!file && <p className="pl-1">or drag and drop</p>}
                </div>
                <p className="text-xs text-slate-500">PDF up to 10MB</p>
              </div>
            </div>
          </div>

          {message && (
            <div
              className={`rounded-md p-3 text-sm ${
                message.type === "success"
                  ? "bg-teal-50 text-teal-800 dark:bg-teal-900/30 dark:text-teal-200"
                  : "bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-200"
              }`}
            >
              {message.text}
            </div>
          )}

          <div className="mt-5 sm:mt-6 sm:grid sm:grid-flow-row-dense sm:grid-cols-2 sm:gap-3">
            <button
              type="submit"
              disabled={!canSubmitUpload || isUploading}
              className="inline-flex w-full justify-center rounded-md border border-transparent bg-teal-600 px-4 py-2 text-base font-medium text-white shadow-sm hover:bg-teal-700 focus:outline-none focus:ring-2 focus:ring-teal-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-400 sm:col-start-2 sm:text-sm"
            >
              {isUploading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              Upload & Process
            </button>
            <button
              type="button"
              onClick={onClose}
              disabled={isUploading}
              className="mt-3 inline-flex w-full justify-center rounded-md border border-slate-300 bg-white px-4 py-2 text-base font-medium text-slate-700 shadow-sm hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-teal-500 focus:ring-offset-2 disabled:cursor-not-allowed dark:border-slate-600 dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600 sm:col-start-1 sm:mt-0 sm:text-sm"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
