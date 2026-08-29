import { lazy, Suspense, useEffect, useState } from "react";
import { Network, ArrowLeft } from "lucide-react";

import AppErrorBoundary from "@/components/AppErrorBoundary";
import DemoAccessGate from "@/components/DemoAccessGate";

const Home = lazy(() => import("@/pages/Home"));
const Dashboard = lazy(() => import("@/pages/Dashboard"));

export type Page = "home" | "dashboard";

export default function App(): JSX.Element {
  const [page, setPage] = useState<Page>(() => getPageFromPath());

  function navigate(nextPage: Page): void {
    const path = nextPage === "dashboard" ? "/dashboard" : "/";
    window.history.pushState({}, "", path);
    setPage(nextPage);
  }

  useEffect(() => {
    const handlePopState = () => setPage(getPageFromPath());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  return (
    <div className="min-h-screen bg-white dark:bg-[#0B0B0F]">
      <nav className="fixed inset-x-0 top-0 z-50 flex h-16 items-center border-b border-slate-200/70 bg-white/85 px-4 shadow-sm backdrop-blur lg:px-6 dark:border-white/10 dark:bg-[#0B0B0F]/80">
        <div className="flex w-full items-center justify-between">
          <div className="flex items-center gap-3">
            {page !== "home" && (
              <button
                onClick={() => navigate("home")}
                className="mr-2 grid h-8 w-8 place-items-center rounded-md border border-slate-200 text-slate-500 transition hover:bg-slate-100 hover:text-ink dark:border-white/10 dark:text-slate-400 dark:hover:bg-white/10 dark:hover:text-white"
                aria-label="Go back to home"
              >
                <ArrowLeft className="h-4 w-4" />
              </button>
            )}
            <button
              onClick={() => navigate("home")}
              className="flex items-center gap-3"
            >
              <div className="grid h-9 w-9 place-items-center rounded-md bg-ink text-sm font-bold text-white dark:bg-teal-400 dark:text-slate-950">
                CG
              </div>
              <div className="text-left">
                <p className="text-sm font-semibold text-ink dark:text-white">
                  ConceptGraph AI Pipeline
                </p>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  Academic graph retrieval dashboard
                </p>
              </div>
              <span className="hidden rounded-full bg-teal-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-teal-700 sm:inline-flex">
                Shared portfolio demo
              </span>
            </button>
          </div>

          <div className="flex items-center gap-2">
            {page === "home" && (
              <button
                onClick={() => navigate("dashboard")}
                className="inline-flex items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-ink shadow-sm transition hover:bg-slate-50 dark:border-white/10 dark:bg-white/5 dark:text-white dark:hover:bg-white/10"
              >
                <Network className="h-3.5 w-3.5" />
                Open Dashboard
              </button>
            )}
          </div>
        </div>
      </nav>
      <div className="pt-16">
        <AppErrorBoundary>
          <Suspense fallback={<div className="grid min-h-[calc(100vh-64px)] place-items-center text-sm text-slate-500">Loading workspace...</div>}>
            {page === "home" && <Home navigate={navigate} />}
            {page === "dashboard" && (
              <DemoAccessGate>
                <Dashboard />
              </DemoAccessGate>
            )}
          </Suspense>
        </AppErrorBoundary>
      </div>
    </div>
  );
}

function getPageFromPath(): Page {
  return window.location.pathname === "/dashboard" ? "dashboard" : "home";
}
