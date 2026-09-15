import { FormEvent, ReactNode, useEffect, useState } from "react";
import { ArrowRight, BookOpen, KeyRound, LoaderCircle, LockKeyhole, ShieldCheck } from "lucide-react";

import {
  createAuthSession,
  deleteAuthSession,
  getAuthSession,
} from "@/services/api";

type AccessState = "checking" | "locked" | "unlocked" | "error";

interface DemoAccessGateProps {
  children: ReactNode;
}

export default function DemoAccessGate({ children }: DemoAccessGateProps): JSX.Element {
  const [state, setState] = useState<AccessState>("checking");
  const [accessCode, setAccessCode] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [protectionEnabled, setProtectionEnabled] = useState(false);

  useEffect(() => {
    const sessionExpired = () => {
      setState("locked");
      setMessage("Your session has expired. Enter the access code to continue.");
      setAccessCode("");
    };
    window.addEventListener("conceptgraph:session-expired", sessionExpired);
    return () => window.removeEventListener("conceptgraph:session-expired", sessionExpired);
  }, []);

  useEffect(() => {
    let active = true;
    getAuthSession()
      .then((session) => {
        if (active) {
          setProtectionEnabled(session.enabled);
          setState(session.authenticated ? "unlocked" : "locked");
        }
      })
      .catch(() => {
        if (active) {
          setState("error");
          setMessage("The API is unavailable, so dashboard access could not be verified.");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  async function unlock(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    try {
      const issuedSession = await createAuthSession(accessCode);
      if (issuedSession.authenticated) {
        const verifiedSession = await getAuthSession();
        if (!verifiedSession.authenticated) {
          throw new Error("The temporary session could not be verified. Please try again.");
        }
        setProtectionEnabled(verifiedSession.enabled);
        setAccessCode("");
        setState("unlocked");
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Dashboard access failed.");
    } finally {
      setSubmitting(false);
    }
  }

  async function lock(): Promise<void> {
    try {
      await deleteAuthSession();
    } finally {
      setState("locked");
    }
  }

  if (state === "checking") {
    return (
      <main className="grid min-h-[calc(100vh-64px)] place-items-center bg-[#f8fafb] p-6 text-sm text-slate-600 dark:bg-[#0B0B0F] dark:text-slate-300">
        <span role="status" className="inline-flex items-center gap-2">
          <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Verifying dashboard access...
        </span>
      </main>
    );
  }

  if (state === "unlocked") {
    if (!protectionEnabled) {
      return <>{children}</>;
    }
    return (
      <div>
        <div className="border-b border-slate-200 bg-slate-50/90 dark:border-white/10 dark:bg-[#111117]">
          <div className="mx-auto flex h-10 w-full max-w-[1800px] items-center justify-between gap-3 px-4 lg:px-6">
            <span className="inline-flex min-w-0 items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
              <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-teal-600 dark:text-teal-400" />
              <span className="truncate">Shared portfolio demo · verified reviewer session</span>
            </span>
            <button
              type="button"
              onClick={() => void lock()}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-xs font-semibold text-slate-600 shadow-sm transition hover:border-slate-300 hover:text-ink dark:border-white/10 dark:bg-white/5 dark:text-slate-300 dark:hover:bg-white/10 dark:hover:text-white"
            >
              <LockKeyhole className="h-3.5 w-3.5" />
              End session
            </button>
          </div>
        </div>
        {children}
      </div>
    );
  }

  return (
    <main className="min-h-[calc(100vh-64px)] bg-[#f8fafb] px-4 py-10 sm:py-16 dark:bg-[#0B0B0F]">
      <div className="mx-auto grid w-full max-w-4xl items-start gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,0.8fr)]">
        <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm sm:p-7 dark:border-white/10 dark:bg-[#15151b]">
          <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.14em] text-teal-700 dark:text-teal-300">
            <ShieldCheck className="h-4 w-4" aria-hidden="true" /> Reviewer access
          </div>
          <h1 className="mt-3 text-2xl font-semibold tracking-tight text-ink dark:text-white">Explore the live workspace</h1>
          <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
            Enter the reviewer code to upload PDFs and use live AI features. This is one shared, temporary portfolio workspace, not a private account.
          </p>

          <form onSubmit={(event) => void unlock(event)} className="mt-6 space-y-3">
            <label className="block text-sm font-semibold text-ink dark:text-white" htmlFor="demo-access-code">Reviewer access code</label>
            <div className="relative">
              <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" aria-hidden="true" />
              <input
                id="demo-access-code"
                type="password"
                autoComplete="current-password"
                value={accessCode}
                onChange={(event) => setAccessCode(event.target.value)}
                aria-describedby={message ? "demo-access-error" : undefined}
                className="h-12 w-full rounded-md border border-slate-300 bg-white pl-10 pr-3 text-base text-ink outline-none transition focus:border-teal-600 focus:ring-2 focus:ring-teal-100 disabled:bg-slate-100 dark:border-white/15 dark:bg-black/20 dark:text-white dark:focus:border-teal-400 dark:focus:ring-teal-400/10 dark:disabled:bg-white/5"
                placeholder="Enter the code you were given"
                required
                disabled={submitting || state === "error"}
              />
            </div>
            {message ? (
              <p id="demo-access-error" role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm leading-5 text-red-700 dark:border-red-400/20 dark:bg-red-400/10 dark:text-red-300">
                {message}
              </p>
            ) : null}
            <button
              type="submit"
              disabled={submitting || state === "error" || !accessCode.trim()}
              className="inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-md bg-ink px-4 text-sm font-semibold text-white transition-colors hover:bg-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-teal-300 dark:text-slate-950 dark:hover:bg-teal-200"
            >
              {submitting ? <LoaderCircle className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
              Verify access
            </button>
          </form>
          {state === "error" ? (
            <button type="button" onClick={() => window.location.reload()} className="mt-4 min-h-10 text-sm font-semibold text-teal-800 underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 dark:text-teal-300">
              Retry API connection
            </button>
          ) : null}
          <p className="mt-5 border-t border-slate-200 pt-4 text-xs leading-5 text-slate-500 dark:border-white/10 dark:text-slate-400">
            Reviewer uploads are visible to other reviewers and are automatically removed after a few days.
          </p>
        </section>

        <aside className="rounded-xl border border-teal-100 bg-teal-50/60 p-5 sm:p-7 dark:border-teal-400/20 dark:bg-teal-400/5">
          <BookOpen className="h-6 w-6 text-teal-700 dark:text-teal-300" aria-hidden="true" />
          <h2 className="mt-4 text-xl font-semibold tracking-tight text-ink dark:text-white">No code? Start with the sample.</h2>
          <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
            Explore saved answers, an illustrated concept path, and clickable source PDFs. It works without a login or live AI request.
          </p>
          <a href="/sample" className="mt-5 inline-flex min-h-11 items-center gap-2 text-sm font-semibold text-teal-800 underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 dark:text-teal-300">
            Explore public sample <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </a>
        </aside>
      </div>
    </main>
  );
}
