import { Hero } from "@/components/ui/animated-hero";
import { MacbookScroll } from "@/components/ui/macbook-scroll";
import type { Page } from "../App";

interface HomeProps {
  navigate: (page: Page) => void;
}

export default function Home({ navigate }: HomeProps): JSX.Element {
  return (
    <main className="min-h-screen overflow-hidden bg-white text-foreground dark:bg-[#0B0B0F]">
      <Hero onTryDemo={() => navigate("dashboard")} />
      <section className="mx-auto max-w-6xl px-4 pb-16 lg:px-6">
        <div className="rounded-xl border border-teal-100 bg-teal-50/60 p-5 text-center dark:border-teal-400/20 dark:bg-teal-400/5">
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Want to see the result first? Open the prepared, read-only course sample with saved answers, citations, and an illustrated graph.
          </p>
          <button
            type="button"
            onClick={() => navigate("sample")}
            className="mt-3 rounded-md bg-teal-700 px-4 py-2 text-sm font-semibold text-white transition hover:bg-teal-800 dark:bg-teal-400 dark:text-slate-950 dark:hover:bg-teal-300"
          >
            Explore the sample course
          </button>
        </div>
      </section>
      <div className="w-full overflow-hidden bg-white dark:bg-[#0B0B0F]">
        <MacbookScroll
          title={
            <span>
              Built with a multi-database architecture. Powered by Neo4j.
            </span>
          }
          src="/dashboard-preview.webp"
          showGradient={false}
        />
      </div>
    </main>
  );
}
