import { FormEvent, ReactNode, useEffect, useState } from "react";
import { KeyRound, LoaderCircle, LockKeyhole, ShieldCheck } from "lucide-react";

import {
  createAuthSession,
  deleteAuthSession,
  getAuthSession,
} from "@/services/api";
import PublicSampleCourse from "@/components/PublicSampleCourse";

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
      <div className="mx-auto max-w-5xl space-y-4 p-4 text-sm text-slate-500">
        <span role="status" className="inline-flex items-center gap-2">
          <LoaderCircle className="h-4 w-4 animate-spin" />
          Verifying dashboard access...
        </span>
        <PublicSampleCourse />
      </div>
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
    <main className="min-h-[calc(100vh-64px)] bg-[radial-gradient(circle_at_top_left,_rgba(13,148,136,0.08),_transparent_35%)] px-4 py-10 dark:bg-[radial-gradient(circle_at_top_left,_rgba(45,212,191,0.09),_transparent_35%)]">
      <div className="mx-auto grid w-full max-w-6xl items-start gap-5 lg:grid-cols-[minmax(320px,400px)_1fr]">
      <section className="w-full rounded-xl border border-slate-200 bg-white p-6 shadow-xl shadow-slate-200/40 dark:border-white/10 dark:bg-[#15151b] dark:shadow-black/30">
        <div className="mb-5 flex items-start gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-teal-50 text-teal-700 dark:bg-teal-400/10 dark:text-teal-300">
            <ShieldCheck className="h-5 w-5" />
          </div>
          <div>
            <h1 className="font-semibold text-ink dark:text-white">Shared portfolio demo</h1>
            <p className="mt-1 text-sm leading-5 text-slate-500 dark:text-slate-400">
              Reviewers can enter the private access code for a temporary verified session. Public visitors can explore the prepared sample course.
            </p>
          </div>
        </div>

        <form onSubmit={(event) => void unlock(event)} className="space-y-3">
          <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400" htmlFor="demo-access-code">
            Access code
          </label>
          <div className="relative">
            <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              id="demo-access-code"
              type="password"
              autoComplete="current-password"
              value={accessCode}
              onChange={(event) => setAccessCode(event.target.value)}
              className="h-11 w-full rounded-md border border-slate-300 bg-white pl-10 pr-3 text-sm text-ink outline-none transition focus:border-teal-600 focus:ring-2 focus:ring-teal-100 dark:border-white/15 dark:bg-black/20 dark:text-white dark:focus:border-teal-400 dark:focus:ring-teal-400/10"
              placeholder="Deployment access code"
              required
              disabled={submitting}
            />
          </div>
          {message && (
            <p role="alert" className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700 dark:border-red-400/20 dark:bg-red-400/10 dark:text-red-300">
              {message}
            </p>
          )}
          <button
            type="submit"
            disabled={submitting || !accessCode.trim()}
            className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-md bg-ink px-4 text-sm font-semibold text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-teal-400 dark:text-slate-950 dark:hover:bg-teal-300"
          >
            {submitting && <LoaderCircle className="h-4 w-4 animate-spin" />}
            Unlock dashboard
          </button>
        </form>

        <p className="mt-4 rounded-md bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-500 dark:bg-white/5 dark:text-slate-400">
          Reviewer uploads are temporary and automatically removed after a few days. This is a controlled demo, not a multi-user account system.
        </p>

        {state === "error" && (
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-3 w-full text-center text-xs font-semibold text-teal-700 hover:underline dark:text-teal-300"
          >
            Retry connection
          </button>
        )}
      </section>
      <PublicSampleCourse />
      </div>
    </main>
  );
}
