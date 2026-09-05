import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { MoveRight, Network } from "lucide-react";

import { Button } from "@/components/ui/button";

const rotatingWords = [
  "intelligent",
  "contextual",
  "syllabus-bounded",
  "graph-driven",
  "fast",
];

interface HeroProps {
  onTryDemo?: () => void;
  onViewSample?: () => void;
}

function Hero({ onTryDemo, onViewSample }: HeroProps): JSX.Element {
  const [titleNumber, setTitleNumber] = useState(0);
  const titles = useMemo(() => rotatingWords, []);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      setTitleNumber((current) => (current === titles.length - 1 ? 0 : current + 1));
    }, 2000);

    return () => window.clearTimeout(timeoutId);
  }, [titleNumber, titles]);

  return (
    <section className="w-full">
      <div className="mx-auto max-w-7xl px-4">
        <div className="flex flex-col items-center justify-center gap-8 py-20 text-center lg:py-28">
          <Button
            variant="secondary"
            size="sm"
            className="gap-3 border border-border bg-white/80 dark:bg-white/10"
          >
            ConceptGraph architecture
            <Network className="h-4 w-4" aria-hidden="true" />
          </Button>

          <div className="flex max-w-4xl flex-col gap-5">
            <h1 className="text-5xl font-semibold tracking-normal text-foreground md:text-7xl">
              <span className="block">Academic RAG that is</span>
              <span className="relative flex min-h-[1.25em] w-full justify-center overflow-hidden text-center text-signal dark:text-teal-300 md:pb-4 md:pt-1">
                {titles.map((title, index) => (
                  <motion.span
                    key={title}
                    className="absolute"
                    initial={{ opacity: 0, y: "-100%" }}
                    transition={{ type: "spring", stiffness: 50 }}
                    animate={
                      titleNumber === index
                        ? { y: 0, opacity: 1 }
                        : {
                            y: titleNumber > index ? "-150%" : "150%",
                            opacity: 0,
                          }
                    }
                  >
                    {title}
                  </motion.span>
                ))}
              </span>
            </h1>

            <p className="mx-auto max-w-2xl text-lg leading-8 text-muted-foreground md:text-xl">
              Studying dense engineering subjects is already tough. Our goal is
              to streamline your learning path by mapping conceptual
              prerequisites.
            </p>
          </div>

          <div className="flex flex-col gap-3 sm:flex-row">
            <Button size="lg" className="gap-3" variant="outline" onClick={onViewSample}>
              View Architecture
              <Network className="h-4 w-4" aria-hidden="true" />
            </Button>
            <Button size="lg" className="gap-3" onClick={onTryDemo}>
              Try Demo
              <MoveRight className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}

export { Hero };
