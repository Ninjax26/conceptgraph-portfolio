import { lazy, Suspense, useEffect, useRef, useState, type MouseEvent } from "react";
import { BookOpen, Network } from "lucide-react";

import AppErrorBoundary from "@/components/AppErrorBoundary";
import DemoAccessGate from "@/components/DemoAccessGate";
import PublicSampleCourse from "@/components/PublicSampleCourse";

const Home = lazy(() => import("@/pages/Home"));
const Dashboard = lazy(() => import("@/pages/Dashboard"));

export type Page = "home" | "dashboard" | "sample";

export default function App(): JSX.Element {
  const [page, setPage] = useState<Page>(() => getPageFromPath());
  const mainRef = useRef<HTMLDivElement>(null);

  function navigate(nextPage: Page): void {
    const path = nextPage === "dashboard" ? "/dashboard" : nextPage === "sample" ? "/sample" : "/";
    window.history.pushState({}, "", path);
    setPage(nextPage);
    window.scrollTo(0, 0);
    window.requestAnimationFrame(() => mainRef.current?.focus());
  }

  function followRoute(event: MouseEvent<HTMLAnchorElement>, nextPage: Page): void {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(nextPage);
  }

  useEffect(() => {
    const handlePopState = () => setPage(getPageFromPath());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    document.title = page === "sample"
      ? "Public sample | ConceptGraph"
      : page === "dashboard"
        ? "Reviewer workspace | ConceptGraph"
        : "ConceptGraph | Source-grounded PDF GraphRAG";
  }, [page]);

  return (
    <div className="min-h-screen bg-white font-sans dark:bg-[#0B0B0F]">
      <a href="#main-content" className="sr-only fixed left-3 top-2 z-[60] rounded-md bg-white px-3 py-2 text-sm font-semibold text-ink shadow-lg focus:not-sr-only focus:outline focus:outline-2 focus:outline-teal-700">
        Skip to content
      </a>
      <nav aria-label="Primary" className="fixed inset-x-0 top-0 z-50 flex h-16 items-center border-b border-slate-200 bg-white/95 px-3 backdrop-blur sm:px-6 dark:border-white/10 dark:bg-[#0B0B0F]/95">
        <div className="mx-auto flex w-full max-w-7xl items-center justify-between gap-2">
          <a href="/" onClick={(event) => followRoute(event, "home")} aria-label="ConceptGraph home" className="flex min-w-0 items-center gap-2 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 sm:gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-ink text-sm font-bold text-white dark:bg-teal-300 dark:text-slate-950">CG</span>
            <span className="hidden min-w-0 text-left sm:block">
              <span className="block truncate text-sm font-semibold text-ink dark:text-white">ConceptGraph</span>
              <span className="block truncate text-xs text-slate-500 dark:text-slate-400">Source-grounded PDF learning</span>
            </span>
          </a>

          <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
            <a href="/sample" onClick={(event) => followRoute(event, "sample")} aria-current={page === "sample" ? "page" : undefined} className="inline-flex min-h-10 items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2.5 text-xs font-semibold text-ink transition-colors hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 sm:px-3 sm:text-sm dark:border-white/15 dark:bg-white/5 dark:text-white dark:hover:bg-white/10">
              <BookOpen className="h-4 w-4" aria-hidden="true" /><span className="sm:hidden">Sample</span><span className="hidden sm:inline">Public sample</span>
            </a>
            <a href="/dashboard" onClick={(event) => followRoute(event, "dashboard")} aria-current={page === "dashboard" ? "page" : undefined} className="inline-flex min-h-10 items-center gap-1.5 rounded-md bg-ink px-2.5 text-xs font-semibold text-white transition-colors hover:bg-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 sm:px-3 sm:text-sm dark:bg-teal-300 dark:text-slate-950 dark:hover:bg-teal-200">
              <Network className="h-4 w-4" aria-hidden="true" /><span className="sm:hidden">Workspace</span><span className="hidden sm:inline">Reviewer workspace</span>
            </a>
          </div>
        </div>
      </nav>
      <div id="main-content" ref={mainRef} tabIndex={-1} className="pt-16">
        <AppErrorBoundary>
          <Suspense fallback={<div className="grid min-h-[calc(100vh-64px)] place-items-center text-sm text-slate-500">Loading workspace...</div>}>
            {page === "home" && <Home navigate={navigate} />}
            {page === "dashboard" && (
              <DemoAccessGate>
                <Dashboard />
              </DemoAccessGate>
            )}
            {page === "sample" && (
              <main className="mx-auto max-w-6xl px-4 py-8 lg:px-6">
                <PublicSampleCourse />
              </main>
            )}
          </Suspense>
        </AppErrorBoundary>
      </div>
    </div>
  );
}

function getPageFromPath(): Page {
  if (window.location.pathname === "/dashboard") return "dashboard";
  if (window.location.pathname === "/sample") return "sample";
  return "home";
}
