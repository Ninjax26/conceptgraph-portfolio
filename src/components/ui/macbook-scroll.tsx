import React, { useRef } from "react";
import {
  motion,
  useReducedMotion,
  useScroll,
  useTransform,
  type MotionValue,
} from "motion/react";
import {
  IconBrightnessDown,
  IconBrightnessUp,
  IconCaretDownFilled,
  IconCaretLeftFilled,
  IconCaretRightFilled,
  IconCaretUpFilled,
  IconChevronUp,
  IconCommand,
  IconMicrophone,
  IconMoon,
  IconPlayerSkipForward,
  IconPlayerTrackNext,
  IconPlayerTrackPrev,
  IconSearch,
  IconTable,
  IconVolume,
  IconVolume2,
  IconVolume3,
  IconWorld,
} from "@tabler/icons-react";

import { cn } from "@/lib/utils";

interface MacbookScrollProps {
  src?: string;
  showGradient?: boolean;
  title?: string | React.ReactNode;
  badge?: React.ReactNode;
}

export function MacbookScroll({
  src,
  showGradient = false,
  title,
  badge,
}: MacbookScrollProps): JSX.Element {
  const ref = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start start", "end start"],
  });
  const reducedMotion = useReducedMotion();

  const scaleX = useTransform(scrollYProgress, [0, 0.3], [1.2, 1.5]);
  const scaleY = useTransform(scrollYProgress, [0, 0.3], [0.6, 1.5]);
  const translate = useTransform(scrollYProgress, [0, 1], [0, -12]);
  const rotate = useTransform(scrollYProgress, [0.1, 0.12, 0.3], [-28, -28, 0]);
  const textTransform = useTransform(scrollYProgress, [0, 0.3], [0, 100]);
  const textOpacity = useTransform(scrollYProgress, [0, 0.2], [1, 0]);

  return (
    <>
    <section className="mx-auto max-w-xl px-4 pb-14 pt-6 md:hidden">
      <h2 className="mb-8 text-center text-2xl font-bold leading-tight text-neutral-900 dark:text-white">{title}</h2>
      <div className="rounded-t-xl border-[7px] border-b-0 border-slate-900 bg-slate-900 shadow-xl">
        <img src={src} alt="ConceptGraph dashboard preview" className="aspect-[16/9] w-full rounded-t-sm object-cover object-top" />
      </div>
      <div className="h-4 rounded-b-xl bg-gradient-to-b from-slate-300 to-slate-500 dark:from-slate-600 dark:to-slate-800" />
      {badge ? <div className="mt-3">{badge}</div> : null}
    </section>
    <section
      ref={ref}
      className="hidden min-h-[110vh] shrink-0 flex-col items-center justify-start pb-24 pt-20 [perspective:800px] md:flex"
    >
      <motion.h2
        style={{ translateY: reducedMotion ? 0 : textTransform, opacity: reducedMotion ? 1 : textOpacity }}
        className="mb-14 max-w-4xl text-center text-3xl font-bold leading-tight text-neutral-900 dark:text-white md:text-5xl"
      >
        {title}
      </motion.h2>

      <Lid
        src={src}
        scaleX={reducedMotion ? 1 : scaleX}
        scaleY={reducedMotion ? 1 : scaleY}
        rotate={reducedMotion ? 0 : rotate}
        translate={reducedMotion ? 0 : translate}
      />

      <div className="relative -z-10 h-[22rem] w-[32rem] overflow-hidden rounded-2xl bg-gray-200 dark:bg-[#272729]">
        <div className="relative h-10 w-full">
          <div className="absolute inset-x-0 mx-auto h-4 w-[80%] bg-[#050505]" />
        </div>
        <div className="relative flex">
          <div className="mx-auto h-full w-[10%] overflow-hidden">
            <SpeakerGrid />
          </div>
          <div className="mx-auto h-full w-[80%]">
            <Keypad />
          </div>
          <div className="mx-auto h-full w-[10%] overflow-hidden">
            <SpeakerGrid />
          </div>
        </div>
        <Trackpad />
        <div className="absolute inset-x-0 bottom-0 mx-auto h-2 w-20 rounded-tl-3xl rounded-tr-3xl bg-gradient-to-t from-[#272729] to-[#050505]" />
        {showGradient ? (
          <div className="absolute inset-x-0 bottom-0 z-50 h-40 w-full bg-gradient-to-t from-white via-white to-transparent dark:from-black dark:via-black" />
        ) : null}
        {badge ? <div className="absolute bottom-4 left-4">{badge}</div> : null}
      </div>
    </section>
    </>
  );
}

export function Lid({
  scaleX,
  scaleY,
  rotate,
  translate,
  src,
}: {
  scaleX: MotionValue<number> | number;
  scaleY: MotionValue<number> | number;
  rotate: MotionValue<number> | number;
  translate: MotionValue<number> | number;
  src?: string;
}): JSX.Element {
  return (
    <div className="relative [perspective:800px]">
      <div
        style={{
          transform: "perspective(800px) rotateX(-25deg) translateZ(0px)",
          transformOrigin: "bottom",
          transformStyle: "preserve-3d",
        }}
        className="relative h-[12rem] w-[32rem] rounded-2xl bg-[#010101] p-2"
      >
        <div
          style={{ boxShadow: "0px 2px 0px 2px #171717 inset" }}
          className="absolute inset-0 flex items-center justify-center rounded-lg bg-[#010101]"
        >
          <ConceptGraphLogo />
        </div>
      </div>
      <motion.div
        style={{
          scaleX,
          scaleY,
          rotateX: rotate,
          translateY: translate,
          transformStyle: "preserve-3d",
          transformOrigin: "top",
        }}
        className="absolute inset-0 h-96 w-[32rem] rounded-2xl bg-[#010101] p-2"
      >
        <div className="absolute inset-0 rounded-lg bg-[#272729]" />
        <img
          src={src}
          alt="ConceptGraph dashboard preview"
          className="absolute inset-0 h-full w-full rounded-lg object-cover object-left-top"
        />
      </motion.div>
    </div>
  );
}

export function Trackpad(): JSX.Element {
  return (
    <div
      className="mx-auto my-1 h-32 w-[40%] rounded-xl"
      style={{ boxShadow: "0px 0px 1px 1px #00000020 inset" }}
    />
  );
}

export function SpeakerGrid(): JSX.Element {
  return (
    <div
      className="mt-2 flex h-40 gap-[2px] px-[0.5px]"
      style={{
        backgroundImage:
          "radial-gradient(circle, #08080A 0.5px, transparent 0.5px)",
        backgroundSize: "3px 3px",
      }}
    />
  );
}

export function Keypad(): JSX.Element {
  const rows = [
    ["esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11"],
    ["~", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "delete"],
    ["tab", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "\\", ""],
    ["caps", "A", "S", "D", "F", "G", "H", "J", "K", "L", ";", "return"],
    ["shift", "Z", "X", "C", "V", "B", "N", "M", ",", ".", "/", "shift"],
  ];

  return (
    <div className="mx-1 h-full rounded-md bg-[#050505] p-1 [transform:translateZ(0)] [will-change:transform]">
      <div className="mb-[2px] flex w-full shrink-0 gap-[2px]">
        <KBtn className="w-10 items-end justify-start pb-[2px] pl-[4px]">
          esc
        </KBtn>
        <KBtn><IconBrightnessDown className="h-[6px] w-[6px]" /><span>F1</span></KBtn>
        <KBtn><IconBrightnessUp className="h-[6px] w-[6px]" /><span>F2</span></KBtn>
        <KBtn><IconTable className="h-[6px] w-[6px]" /><span>F3</span></KBtn>
        <KBtn><IconSearch className="h-[6px] w-[6px]" /><span>F4</span></KBtn>
        <KBtn><IconMicrophone className="h-[6px] w-[6px]" /><span>F5</span></KBtn>
        <KBtn><IconMoon className="h-[6px] w-[6px]" /><span>F6</span></KBtn>
        <KBtn><IconPlayerTrackPrev className="h-[6px] w-[6px]" /><span>F7</span></KBtn>
        <KBtn><IconPlayerSkipForward className="h-[6px] w-[6px]" /><span>F8</span></KBtn>
        <KBtn><IconPlayerTrackNext className="h-[6px] w-[6px]" /><span>F9</span></KBtn>
        <KBtn><IconVolume3 className="h-[6px] w-[6px]" /><span>F10</span></KBtn>
        <KBtn><IconVolume2 className="h-[6px] w-[6px]" /><span>F11</span></KBtn>
        <KBtn><IconVolume className="h-[6px] w-[6px]" /><span>F12</span></KBtn>
      </div>

      {rows.slice(1).map((row, rowIndex) => (
        <div className="mb-[2px] flex w-full shrink-0 gap-[2px]" key={rowIndex}>
          {row.map((keyLabel, keyIndex) => (
            <KBtn className={keyWidthClass(keyLabel)} key={`${keyLabel}-${keyIndex}`}>
              {keyLabel}
            </KBtn>
          ))}
        </div>
      ))}

      <div className="mb-[2px] flex w-full shrink-0 gap-[2px]">
        <KBtn childrenClassName="h-full justify-between py-[4px]">
          <span className="self-end pr-1">fn</span>
          <IconWorld className="ml-1 h-[6px] w-[6px] self-start" />
        </KBtn>
        <KBtn childrenClassName="h-full justify-between py-[4px]">
          <IconChevronUp className="mr-1 h-[6px] w-[6px] self-end" />
          <span className="self-start pl-1">control</span>
        </KBtn>
        <KBtn childrenClassName="h-full justify-between py-[4px]">
          <OptionKey className="mr-1 h-[6px] w-[6px] self-end" />
          <span className="self-start pl-1">option</span>
        </KBtn>
        <KBtn className="w-8" childrenClassName="h-full justify-between py-[4px]">
          <IconCommand className="mr-1 h-[6px] w-[6px] self-end" />
          <span className="self-start pl-1">command</span>
        </KBtn>
        <KBtn className="w-[8.2rem]" />
        <KBtn className="w-8" childrenClassName="h-full justify-between py-[4px]">
          <IconCommand className="ml-1 h-[6px] w-[6px] self-start" />
          <span className="self-start pl-1">command</span>
        </KBtn>
        <KBtn childrenClassName="h-full justify-between py-[4px]">
          <OptionKey className="ml-1 h-[6px] w-[6px] self-start" />
          <span className="self-start pl-1">option</span>
        </KBtn>
        <div className="mt-[2px] flex h-6 w-[4.9rem] flex-col items-center justify-end rounded-[4px] p-[0.5px]">
          <KBtn className="h-3 w-6"><IconCaretUpFilled className="h-[6px] w-[6px]" /></KBtn>
          <div className="flex">
            <KBtn className="h-3 w-6"><IconCaretLeftFilled className="h-[6px] w-[6px]" /></KBtn>
            <KBtn className="h-3 w-6"><IconCaretDownFilled className="h-[6px] w-[6px]" /></KBtn>
            <KBtn className="h-3 w-6"><IconCaretRightFilled className="h-[6px] w-[6px]" /></KBtn>
          </div>
        </div>
      </div>
    </div>
  );
}

export function KBtn({
  className,
  children,
  childrenClassName,
  backlit = true,
}: {
  className?: string;
  children?: React.ReactNode;
  childrenClassName?: string;
  backlit?: boolean;
}): JSX.Element {
  return (
    <div
      className={cn(
        "rounded-[4px] p-[0.5px] [transform:translateZ(0)] [will-change:transform]",
        backlit && "bg-white/[0.2] shadow-xl shadow-white",
      )}
    >
      <div
        className={cn(
          "flex h-6 w-6 items-center justify-center rounded-[3.5px] bg-[#0A090D]",
          className,
        )}
        style={{
          boxShadow:
            "0px -0.5px 2px 0 #0D0D0F inset, -0.5px 0px 2px 0 #0D0D0F inset",
        }}
      >
        <div
          className={cn(
            "flex w-full flex-col items-center justify-center text-[5px] text-neutral-200",
            childrenClassName,
            backlit && "text-white",
          )}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

export function OptionKey({ className }: { className: string }): JSX.Element {
  return (
    <svg fill="none" viewBox="0 0 32 32" className={className}>
      <rect stroke="currentColor" strokeWidth={2} x="18" y="5" width="10" height="2" />
      <polygon
        stroke="currentColor"
        strokeWidth={2}
        points="10.6,5 4,5 4,7 9.4,7 18.4,27 28,27 28,25 19.6,25"
      />
    </svg>
  );
}

function ConceptGraphLogo(): JSX.Element {
  return (
    <div className="grid h-8 w-8 place-items-center rounded-md border border-white/20 text-[10px] font-bold text-white">
      CG
    </div>
  );
}

function keyWidthClass(label: string): string {
  if (label === "delete" || label === "tab") {
    return "w-10";
  }
  if (label === "caps" || label === "return") {
    return "w-[2.85rem]";
  }
  if (label === "shift") {
    return "w-[3.65rem]";
  }
  if (!label) {
    return "w-10";
  }
  return "";
}
