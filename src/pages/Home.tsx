import { useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { ArrowRight, BookOpen, FileText, GitBranch, ShieldCheck } from "lucide-react";

import { Hero } from "@/components/ui/animated-hero";
import { MacbookScroll } from "@/components/ui/macbook-scroll";
import type { Page } from "../App";

interface HomeProps {
  navigate: (page: Page) => void;
}

type HomeTab = "showcase" | "overview";

export default function Home({ navigate }: HomeProps): JSX.Element {
  const [activeTab, setActiveTab] = useState<HomeTab>("showcase");
  const showcaseTabRef = useRef<HTMLButtonElement>(null);
  const overviewTabRef = useRef<HTMLButtonElement>(null);

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>): void {
    const nextTab = event.key === "ArrowRight" || event.key === "End"
      ? "overview"
      : event.key === "ArrowLeft" || event.key === "Home"
        ? "showcase"
        : null;
    if (!nextTab) return;
    event.preventDefault();
    setActiveTab(nextTab);
    (nextTab === "showcase" ? showcaseTabRef : overviewTabRef).current?.focus();
  }

  function followRoute(event: MouseEvent<HTMLAnchorElement>, page: Page): void {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    navigate(page);
  }

  const sampleLink = (event: MouseEvent<HTMLAnchorElement>) => followRoute(event, "sample");
  const dashboardLink = (event: MouseEvent<HTMLAnchorElement>) => followRoute(event, "dashboard");

  return (
    <main className={`min-h-screen text-ink dark:bg-[#0B0B0F] dark:text-white ${activeTab === "showcase" ? "bg-white" : "bg-[#f8fafb]"}`}>
      <div className="mx-auto max-w-7xl px-4 pt-6 sm:px-6 lg:px-8">
        <div role="tablist" aria-label="Home page views" className="inline-flex max-w-full gap-1 rounded-xl border border-slate-200 bg-white p-1 shadow-sm dark:border-white/15 dark:bg-white/5">
          <button
            ref={showcaseTabRef}
            id="home-tab-showcase"
            type="button"
            role="tab"
            aria-controls="home-panel-showcase"
            aria-selected={activeTab === "showcase"}
            tabIndex={activeTab === "showcase" ? 0 : -1}
            onClick={() => setActiveTab("showcase")}
            onKeyDown={handleTabKeyDown}
            className={`min-h-11 rounded-lg px-4 text-sm font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 ${activeTab === "showcase" ? "bg-ink text-white dark:bg-teal-300 dark:text-slate-950" : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/10"}`}
          >
            Showcase
          </button>
          <button
            ref={overviewTabRef}
            id="home-tab-overview"
            type="button"
            role="tab"
            aria-controls="home-panel-overview"
            aria-selected={activeTab === "overview"}
            tabIndex={activeTab === "overview" ? 0 : -1}
            onClick={() => setActiveTab("overview")}
            onKeyDown={handleTabKeyDown}
            className={`min-h-11 rounded-lg px-4 text-sm font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 ${activeTab === "overview" ? "bg-ink text-white dark:bg-teal-300 dark:text-slate-950" : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/10"}`}
          >
            Project overview
          </button>
        </div>
      </div>

      {activeTab === "showcase" ? (
        <div id="home-panel-showcase" role="tabpanel" aria-labelledby="home-tab-showcase" tabIndex={0}>
          <Hero onTryDemo={() => navigate("dashboard")} onViewSample={() => setActiveTab("overview")} />
          <section className="mx-auto max-w-6xl px-4 pb-16 lg:px-6">
            <div className="rounded-xl border border-teal-100 bg-teal-50/60 p-5 text-center dark:border-teal-400/20 dark:bg-teal-400/5">
              <p className="text-sm text-slate-600 dark:text-slate-300">
                Want to see the result first? Open the prepared, read-only course sample with saved answers, citations, and an illustrated graph.
              </p>
              <a href="/sample" onClick={sampleLink} className="mt-3 inline-flex min-h-11 items-center rounded-md bg-teal-700 px-4 py-2 text-sm font-semibold text-white transition hover:bg-teal-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 dark:bg-teal-400 dark:text-slate-950 dark:hover:bg-teal-300">
                Explore the sample course
              </a>
            </div>
          </section>
          <div className="w-full overflow-hidden bg-white dark:bg-[#0B0B0F]">
            <MacbookScroll
              title={<span>Built with a multi-database architecture. Powered by Neo4j.</span>}
              src="/dashboard-preview.webp"
              showGradient={false}
            />
          </div>
        </div>
      ) : (
      <div id="home-panel-overview" role="tabpanel" aria-labelledby="home-tab-overview" tabIndex={0}>
      <section className="mx-auto grid max-w-7xl gap-10 px-4 pb-16 pt-12 sm:px-6 lg:grid-cols-[minmax(0,0.93fr)_minmax(0,1.07fr)] lg:items-center lg:gap-14 lg:px-8 lg:pb-20 lg:pt-20">
        <div className="min-w-0">
          <p className="inline-flex items-center gap-2 text-xs font-bold uppercase tracking-[0.16em] text-teal-800 dark:text-teal-300">
            <span className="h-2 w-2 rounded-full bg-teal-600" aria-hidden="true" />
            Source-grounded GraphRAG
          </p>
          <h1 className="mt-5 max-w-[13ch] text-[clamp(2.65rem,5vw,4.7rem)] font-semibold leading-[1.05] tracking-normal">
            See how ideas connect.
            <span className="block text-teal-700 dark:text-teal-300">Check where they came from.</span>
          </h1>
          <p className="mt-6 max-w-xl text-base leading-7 text-slate-600 sm:text-lg sm:leading-8 dark:text-slate-300">
            ConceptGraph turns course PDFs into a concept map and answers questions with source-page citations. Compare vector search with one- and two-hop graph retrieval.
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
            <a href="/sample" onClick={sampleLink} className="inline-flex min-h-12 items-center justify-center gap-2 rounded-lg bg-ink px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-3 focus-visible:outline-teal-700 dark:bg-teal-300 dark:text-slate-950 dark:hover:bg-teal-200">
              Explore the no-login sample <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </a>
            <a href="/dashboard" onClick={dashboardLink} className="inline-flex min-h-12 items-center justify-center rounded-lg border border-slate-300 bg-white px-5 py-3 text-sm font-semibold text-ink transition-colors hover:border-slate-400 hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-3 focus-visible:outline-teal-700 dark:border-white/20 dark:bg-white/5 dark:text-white dark:hover:bg-white/10">
              Open reviewer workspace
            </a>
          </div>
          <p className="mt-4 text-sm leading-6 text-slate-500 dark:text-slate-400">
            The sample is read-only and works without AI quota. Live uploads require a reviewer code and use a shared workspace.
          </p>
        </div>

        <div className="min-w-0 rounded-2xl border border-slate-200 bg-white p-4 shadow-[0_22px_60px_-38px_rgba(15,23,42,0.34)] sm:p-6 dark:border-white/10 dark:bg-[#15151b]">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-4 dark:border-white/10">
            <div>
              <p className="text-[11px] font-bold uppercase tracking-[0.16em] text-teal-700 dark:text-teal-300">Prepared sample</p>
              <h2 className="mt-1 text-lg font-semibold tracking-normal">Computing Foundations</h2>
            </div>
            <span className="inline-flex items-center gap-1.5 rounded-md bg-slate-100 px-2.5 py-1.5 text-xs font-medium text-slate-600 dark:bg-white/10 dark:text-slate-300">
              <BookOpen className="h-3.5 w-3.5" aria-hidden="true" /> Read-only
            </span>
          </div>
          <div className="pt-5">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Question</p>
            <p className="mt-1 text-lg font-semibold leading-7">What should I learn before HTTPS?</p>
          </div>
          <div className="mt-5 rounded-xl border border-teal-100 bg-teal-50/50 p-4 sm:p-5 dark:border-teal-400/20 dark:bg-teal-400/5">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-teal-800 dark:text-teal-300">
              <GitBranch className="h-4 w-4" aria-hidden="true" /> Prerequisite path
            </div>
            <ol className="mt-4 flex flex-col items-stretch gap-2 sm:flex-row sm:items-center" aria-label="HTTP, then TLS, then HTTPS">
              {["HTTP", "TLS", "HTTPS"].map((concept, index) => (
                <li key={concept} className="flex min-w-0 flex-1 items-center gap-2">
                  <span className="flex min-h-11 min-w-0 flex-1 items-center justify-center rounded-lg border border-teal-200 bg-white px-3 text-sm font-semibold text-ink shadow-sm dark:border-teal-400/25 dark:bg-[#15151b] dark:text-white">
                    {concept}
                  </span>
                  {index < 2 ? <ArrowRight className="h-4 w-4 shrink-0 rotate-90 text-teal-700 sm:rotate-0 dark:text-teal-300" aria-hidden="true" /> : null}
                </li>
              ))}
            </ol>
            <p className="mt-4 text-sm leading-6 text-slate-700 dark:text-slate-200">
              Start with HTTP, then TLS, then HTTPS. TLS is the direct prerequisite; HTTP is the foundational concept two steps away.
            </p>
          </div>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2.5 text-sm dark:border-white/10">
            <span className="inline-flex min-w-0 items-center gap-2 text-slate-600 dark:text-slate-300">
              <FileText className="h-4 w-4 shrink-0 text-teal-700 dark:text-teal-300" aria-hidden="true" />
              <span className="truncate">web-foundations.pdf · pages 1-3</span>
            </span>
            <a href="/sample" onClick={sampleLink} className="font-semibold text-teal-800 underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 dark:text-teal-300">
              Inspect sources
            </a>
          </div>
        </div>
      </section>

      <section className="border-y border-slate-200 bg-white dark:border-white/10 dark:bg-[#111116]" aria-labelledby="workflow-title">
        <div className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
          <h2 id="workflow-title" className="text-2xl font-semibold tracking-normal sm:text-3xl">From document to defensible answer</h2>
          <div className="mt-7 grid gap-7 md:grid-cols-3 md:gap-10">
            {[
              ["01 / INGEST", "Preserve the source", "Extract text and headings from each PDF page, with OCR only when native text is sparse."],
              ["02 / CONNECT", "Validate the graph", "Keep only supported concepts and relationships, each linked to its source page."],
              ["03 / ANSWER", "Show the evidence", "Compare retrieval modes and cite the passages used to answer a question."],
            ].map(([step, title, description]) => (
              <div key={step} className="border-l-2 border-teal-600 pl-4">
                <span className="text-xs font-bold tracking-wide text-teal-700 dark:text-teal-300">{step}</span>
                <h3 className="mt-2 text-base font-semibold">{title}</h3>
                <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">{description}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="mx-auto grid max-w-7xl gap-5 px-4 py-12 sm:px-6 lg:grid-cols-[minmax(0,0.75fr)_minmax(0,1.25fr)] lg:items-start lg:gap-12 lg:px-8 lg:py-16" aria-labelledby="evaluation-title">
        <div className="flex items-center gap-3 text-teal-800 dark:text-teal-300">
          <ShieldCheck className="h-6 w-6" aria-hidden="true" />
          <h2 id="evaluation-title" className="text-2xl font-semibold tracking-normal text-ink dark:text-white">Measured, not assumed</h2>
        </div>
        <div>
          <p className="text-base leading-7 text-slate-600 dark:text-slate-300">
            In a small 22-question, three-PDF evaluation, graph expansion found all required sources in 17/17 raw top-five results, compared with 16/17 for vector search alone. The final evidence-gated score was unchanged. The graph added latency, so its value is visible rather than overstated.
          </p>
          <a href="https://github.com/Ninjax26/conceptgraph-portfolio/blob/main/evaluation/ablation-provider-current.md" target="_blank" rel="noopener noreferrer" className="mt-4 inline-flex min-h-11 items-center gap-2 text-sm font-semibold text-teal-800 underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 dark:text-teal-300">
            Read the evaluation <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </a>
        </div>
      </section>
      </div>
      )}
    </main>
  );
}
