# Roadmap — planned, not scheduled

2B is dormant; this file is a promise about the *direction* of any revival, written down so it survives the dormancy. Nothing here is scheduled. The active project is [ClydeCLI](https://github.com/dea6cat/ClydeCLI); items below are for the day 2B resumes.

## 1. Replace the TUI with a Clyde-style CLI

The full-screen Textual TUI was the right interface for proving the thesis; it is not the right interface for what comes next. On revival, 2B moves to a lean terminal REPL — the interaction model ClydeCLI settled on (streaming replies, visible tool calls, permission asks in the flow) — while keeping what made 2B work: the frozen tool surface, native transport, and host-side absorption of complexity.

Scope of the swap: `ui/` is replaced; `core/`, `tooling/`, `providers/`, `evals/` carry over. The TUI will be kept on a branch for reference, not deleted.

## 2. Local-model performance on consumer machines

The target machine is the 16 GB consumer class — an **Apple M5 Air 16 GB** is the reference point. The problem: what is the best agentic experience extractable from that hardware, measured rather than guessed.

What is already known from measurement (see ClydeCLI's `docs/research/local-models.md` for the numbers, measured on the 18 GB machine just above this class):

- **Dense ≤8B models are the usable class** on 16–18 GB; a ~12 GB MoE is not (swap, sub-1 tok/s, and mmap does not rescue it).
- **KV-cache and quantization settings need a per-model quality gate** — the same setting passed one 3B and failed another. The gate design (`clyde tune`'s buried-fact recall probe) carries over directly.
- On 16 GB specifically, the memory budget is tighter than the machine those numbers came from: expect dense 7–8B at 4-bit, or 3–4B at 8-bit, as the working set. This needs its own measurements, which is exactly the work item.

Open questions for the revival:

1. **A measured 16 GB budget table** — same method as ClydeCLI's `/models local` ratings (relax / balance / hard), tuned to the M5 Air class.
2. **Quality-gated quantization** — port `clyde tune`'s per-model gate so a 4-bit recommendation is never shipped without a recall probe.
3. **Fine-tuning as the floor-raiser** — the dataset and validation method exist (see ClydeCLI's `docs/research/fine-tuning-data.md`); a consumer-trainable configuration (LoRA on ≤8B) is the missing piece.
4. **Publish the anti-shim evidence** — the thesis this repo exists for has internal measurements and zero published ones; the first act of the revival should be the benchmark write-up.

## 3. Relationship to ClydeCLI

2B and ClydeCLI are one research program in two repos. 2B states the thesis in its purest form; ClydeCLI scales it to every model. When 2B resumes, it resumes as the focused, consumer-hardware end of that program — not as a second general harness.
