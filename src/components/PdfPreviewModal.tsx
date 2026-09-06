import { X, ExternalLink } from "lucide-react";

interface PdfPreviewModalProps {
  isOpen: boolean;
  onClose: () => void;
  previewUrl: string;
  title: string;
}

export default function PdfPreviewModal({
  isOpen,
  onClose,
  previewUrl,
  title,
}: PdfPreviewModalProps): JSX.Element | null {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/70 p-2 backdrop-blur-sm sm:p-4">
      <div className="flex h-[85vh] w-full max-w-6xl flex-col overflow-hidden rounded-md border border-slate-200 bg-white shadow-2xl dark:border-white/10 dark:bg-[#0B0B0F]">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-white/10">
          <div className="min-w-0 flex-1 basis-40">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              PDF Preview
            </p>
            <h2 className="truncate text-sm font-semibold text-slate-900 dark:text-white">
              {title}
            </h2>
          </div>
          <div className="flex items-center gap-2">
            <a
              className="inline-flex items-center gap-2 rounded-md border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:bg-slate-50 dark:border-white/10 dark:text-slate-200 dark:hover:bg-white/5"
              href={previewUrl}
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink className="h-4 w-4" />
              Open in new tab
            </a>
            <button
              onClick={onClose}
              className="grid h-9 w-9 place-items-center rounded-md border border-slate-200 text-slate-500 transition hover:bg-slate-50 hover:text-slate-900 dark:border-white/10 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-white"
              aria-label="Close preview"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <iframe
          className="min-h-0 w-full flex-1 bg-slate-100 dark:bg-black"
          src={previewUrl}
          title={title}
        />
      </div>
    </div>
  );
}
