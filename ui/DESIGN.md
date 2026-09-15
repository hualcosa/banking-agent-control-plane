---
name: Banking Agent Control Plane
description: A quiet, inspectable financial control surface where agents propose and deterministic systems decide.
colors:
  page-ground: "light-dark(#f4f7f5, #08120f)"
  surface: "light-dark(#ffffff, #0d1915)"
  surface-recessed: "light-dark(#e8f0ec, #14251f)"
  ink: "light-dark(#10231c, #e5f2ec)"
  ink-muted: "light-dark(#50645b, #9bada5)"
  separator: "light-dark(#d5dfda, #20352d)"
  control-boundary: "light-dark(#779488, #668a7b)"
  mineral-authority: "light-dark(#0f6b4f, #4fe0a4)"
  on-authority: "light-dark(#ffffff, #062117)"
  authority-soft: "light-dark(#dff2ea, #12382b)"
  model-trace: "light-dark(#155eb8, #7db3ff)"
  tool-amber: "light-dark(#9a4b08, #f2b45f)"
  refusal-red: "light-dark(#b91c1c, #f87171)"
  refusal-soft: "light-dark(#fef2f2, #2d1618)"
typography:
  display:
    fontFamily: '"Manrope", ui-sans-serif, system-ui, "Segoe UI", Helvetica, Arial, sans-serif'
    fontSize: "clamp(2.35rem, 6vw, 4.7rem)"
    fontWeight: 800
    lineHeight: 0.96
    letterSpacing: "-0.04em"
  headline:
    fontFamily: '"Manrope", ui-sans-serif, system-ui, "Segoe UI", Helvetica, Arial, sans-serif'
    fontSize: "clamp(1.35rem, 2.2vw, 2rem)"
    fontWeight: 800
    lineHeight: 1.08
    letterSpacing: "-0.015em"
  title:
    fontFamily: '"Manrope", ui-sans-serif, system-ui, "Segoe UI", Helvetica, Arial, sans-serif'
    fontSize: "1rem"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.015em"
  body:
    fontFamily: '"Manrope", ui-sans-serif, system-ui, "Segoe UI", Helvetica, Arial, sans-serif'
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "normal"
  label:
    fontFamily: '"IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace'
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "normal"
rounded:
  sm: "6px"
  md: "10px"
  lg: "16px"
  full: "999px"
spacing:
  1: "4px"
  2: "8px"
  3: "12px"
  4: "16px"
  5: "24px"
  6: "32px"
  7: "48px"
components:
  button-primary:
    backgroundColor: "{colors.mineral-authority}"
    textColor: "{colors.on-authority}"
    rounded: "{rounded.full}"
    size: "36px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  field-composer:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "8px"
  status-authority:
    backgroundColor: "{colors.authority-soft}"
    textColor: "{colors.mineral-authority}"
    rounded: "{rounded.full}"
    padding: "2px 8px"
  card-scenario:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "16px"
  card-scenario-featured:
    backgroundColor: "{colors.authority-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "clamp(20px, 3vw, 34px)"
  message-user:
    backgroundColor: "{colors.surface-recessed}"
    textColor: "{colors.ink}"
    rounded: "16px 16px 6px 16px"
    padding: "12px 16px"
  message-refused:
    backgroundColor: "{colors.refusal-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "12px 16px"
---

# Design System: Banking Agent Control Plane

## Overview

**Creative North Star: "The Quiet Control Room"**

This system is restrained, precise, and technically credible: a financial operations surface that makes authority visible without impersonating a bank dashboard or turning observability into decoration. Quiet mineral grounds and deliberate whitespace let the evidence lead. Green identifies deterministic authority, blue traces model activity, amber identifies tools and risk-bearing steps, and red is reserved for refusals and failures.

The conversation remains familiar, but it is not the whole product. The first-use scenario lab offers realistic PIX and BRL paths into the mocked Brazilian bank; after a turn, the pipeline rail sits directly beneath the answer it measures. The UI is English, while Brazilian names, currency formatting, and payment context remain authentic and are briefly explained where needed.

**Key Characteristics:**

- Calm financial surfaces with dense instrumentation only where evidence demands it.
- Asymmetric conversation: the user is bubbled; agent output remains on the page.
- A prominent scenario lab for first use, followed by an inspectable transcript.
- Semantic signal colors reinforced by labels, glyphs, weight, and boundaries.
- Light and dark themes generated from the same semantic tokens.

## Colors

The palette combines mineral green authority with cool green-grey neutrals, using blue, amber, and red as narrow semantic signals rather than decoration.

### Primary

- **Mineral Authority:** Identifies the control plane, active state, send action, guard stages, selection, and focus.
- **Authority Wash:** Gives high-value control states and the featured scenario a quiet green field without competing with text.
- **On Authority:** Maintains legible content on filled authority controls in both themes.

### Secondary

- **Model Trace Blue:** Marks model-stage evidence and trace links so probabilistic activity is distinct from deterministic authority.

### Tertiary

- **Tool Amber:** Marks tool activity and risk-bearing scenario signals; it is not a general highlight color.
- **Refusal Red:** Owns blocked stages, violations, refusals, failures, and guard-path signals.
- **Refusal Wash:** Contains refusal and failure detail while keeping long evidence readable.

### Neutral

- **Page Ground:** The quiet application canvas behind panels and the transcript.
- **Surface:** The main reading plane for the scenario lab, sidebar, composer, and code blocks.
- **Recessed Surface:** User messages, hover feedback, and inline machine fragments.
- **Ink:** Primary prose and labels.
- **Muted Ink:** Metadata, explanations, placeholders, and inactive instrumentation.
- **Separator:** Low-contrast division between regions; it is intentionally too quiet for control boundaries.
- **Control Boundary:** The visible edge for fields and pointer targets.

### Named Rules

**The Semantic Signal Rule.** Green means authority, blue means model or trace activity, amber means tool or risk context, and red means refusal or failure; never spend these colors as decoration.

**The Two-Border Rule.** Separators use the quiet line, while every control a reader must find or aim at uses the stronger boundary.

**The Redundancy Rule.** Skip and blocked states never rely on color alone; pair color with distinct copy, glyphs, strike-through, weight, or a full boundary.

## Typography

**Display Font:** Manrope (with system sans-serif fallbacks)

**Body Font:** Manrope (with system sans-serif fallbacks)
**Label/Mono Font:** IBM Plex Mono (with system monospace fallbacks)

**Character:** Manrope brings compact, contemporary authority without corporate stiffness. IBM Plex Mono gives runtime evidence a visibly different voice and keeps changing numeric values stable with tabular figures.

### Hierarchy

- **Display** (800, fluid oversized scale, 0.96 line-height): The scenario-lab proposition only; balance it and keep it to roughly eleven characters per line.
- **Headline** (800, fluid compact scale, 1.08 line-height): Featured scenario titles and other rare high-emphasis operational choices.
- **Title** (700, compact scale, 1.25 line-height): Scenario names and compact section-level labels.
- **Body** (400, base scale, 1.6 line-height): Conversation and explanatory copy, constrained to the 48rem transcript measure.
- **Label** (500, compact scale): Pipeline stages, durations, costs, thread identifiers, and other machine facts; use tabular figures.

### Named Rules

**The Evidence Voice Rule.** Human-facing explanation uses Manrope; identifiers, timing, token counts, cost, and control evidence use IBM Plex Mono.

**The One-Display Rule.** Oversized type belongs to the first-use proposition, not routine transcript or navigation content.

## Layout

The application has three regions: conversation history, topbar, and a main column containing the transcript plus composer. On wide screens the 16rem sidebar shares the viewport with the main column. Below 1024px it becomes a modal overlay up to 20rem or 85vw and starts closed; the wide-layout preference must not cover the first mobile view.

The transcript is a centered 48rem reading column. The first-use scenario lab expands to 66rem so its decision paths can form an asymmetric two-column composition: one featured control path spanning two rows beside two compact alternatives. At 560px and below, the lab, control map, and scenario cards collapse to one column; decorative arrows disappear, while controls retain a minimum 44px target.

Spacing follows a 4px base with seven implemented steps. Use tighter 4–12px gaps inside instrumentation and controls, 16–24px for component padding, and 32–48px between transcript-level ideas. Output regions never receive fixed heights; long agent prose and ASCII diagrams must scroll only where the content itself requires horizontal overflow.

**The Evidence-Next-to-Effect Rule.** The rail belongs directly below the answer it measures; never move it into a detached drawer that forces visual correlation.

**The Mobile-Starts-Clear Rule.** Overlay navigation begins closed on narrow screens and closes after selection so the conversation remains the immediate task.

## Elevation & Depth

The system is flat by default and uses tonal layering, borders, and whitespace for most hierarchy. The scenario lab is the one ambiently lifted surface (`0 18px 48px color-mix(in srgb, var(--text) 10%, transparent)`), marking it as a temporary first-use stage. The narrow sidebar receives a directional shadow (`12px 0 32px rgb(0 0 0 / 0.2)`) only while it overlays content. Focus uses a three-pixel authority wash around the composer; hover motion lifts scenario cards by two pixels.

### Shadow Vocabulary

- **Scenario Ambient:** A broad, low-opacity shadow for the first-use lab only.
- **Drawer Directional:** A right-cast shadow that separates the temporary mobile sidebar from its dimmed backdrop.
- **Composer Focus Halo:** A soft three-pixel authority ring that reinforces, but does not replace, the visible control boundary.

### Named Rules

**The Flat-by-Default Rule.** Resting operational surfaces are separated by tone and line; elevation appears only for the first-use stage, temporary overlay, focus, or responsive hover.

## Shapes

Corners are gently technical, not pillowy: 6px for small code fragments and compact affordances, 10px for ordinary controls and cards, and 16px for the major scenario surface, composer, and user message. Full pills are limited to compact status metadata and the circular send action. User messages use an asymmetric 16px/6px silhouette to point back toward the transcript, while agent output remains uncontained.

Borders are one pixel and semantic. A refused answer gains a complete red boundary and wash so it survives grayscale and cannot be mistaken for ordinary output. Circular signal dots and square agent marks are small indicators, never the sole state carrier.

**The Restrained-Radius Rule.** Use the existing four-step radius vocabulary; do not turn cards, panels, and navigation rows into interchangeable capsules.

## Components

### Buttons

- **Shape:** The primary send action is a 36px circle; secondary actions use gently curved 10px corners and compact 8px by 12px padding.
- **Primary:** Mineral authority fill with on-authority ink; disabled state drops to 40% opacity and changes the cursor.
- **Hover / Focus:** Secondary and icon actions receive a recessed-surface hover. All interactive elements keep a visible two-pixel authority focus ring with two-pixel offset.
- **Secondary / Ghost:** New-conversation uses a strong visible boundary; icon actions stay transparent until hover.

### Chips

- **Style:** Header facts are compact mono pills with quiet borders. The guardrails dial uses authority text, authority wash, and the strong boundary.
- **State:** Guardrails-off switches to refusal red, refusal wash, and a red boundary so a screenshot cannot conceal that the gates are down.

### Cards / Containers

- **Corner Style:** Scenario cards use 10px corners; the enclosing scenario lab uses 16px.
- **Background:** Ordinary paths stay on the base surface; the featured confirmation path uses the authority wash.
- **Shadow Strategy:** Cards are flat; only the enclosing first-use lab receives ambient elevation.
- **Border:** One-pixel quiet borders at rest, strong boundaries on the featured and hovered paths.
- **Internal Padding:** 16px for compact paths and a fluid 20–34px for the featured path.

### Inputs / Fields

- **Style:** A borderless, auto-growing 16px textarea sits inside a strongly bounded 16px composer surface.
- **Focus:** The container receives the authority halo while the global focus-visible outline remains available to other controls.
- **Error / Disabled:** Disabled input is 60% opaque; the send action is 40% opaque. In-flight state replaces the arrow with a labeled processing spinner.

### Navigation

Conversation rows are single-line, left-aligned, and ellipsized. The active conversation uses mineral authority and semibold weight; delete remains visually quiet until row hover or keyboard focus-within, then turns red on hover. Mobile navigation is a focus-trapped modal drawer with a dim backdrop, explicit close action, Escape support, and focus return.

### Scenario Lab

The lab is the first-use surface, not an evaluation report. Each scenario names a real behavior, shows the exact prompt it prepares, and explains the evidence to watch; choosing one fills the composer but never sends automatically. The control map compresses the product thesis into “Agent proposes → Plane decides → Bank executes.”

### Asymmetric Messages

User turns are right-aligned recessed bubbles. Agent turns remain unbubbled as the system output surface, with a compact square authority mark and label. Refusals are a third treatment: red label and mark, a full red-bounded answer, complete violation details, and a blocked pipeline glyph.

### Pipeline Rail

The rail is an arrival-ordered wrapping list, never a fixed grid. Each cell contains a distinct status glyph, service-provided label, and honest duration; guard marks are green, model marks blue, and tool marks amber. Skipped stages remain visible and struck through. Blocked stages use a red `✗`. Totals report model usage and the turn's own wall time, while trace links use model-trace blue.

**The No-Reconstruction Rule.** Render service labels and arrival order exactly as received; never sort stages, translate labels locally, or fabricate the rail for reopened conversations whose live frames were not stored.

## Do's and Don'ts

### Do:

- **Do** let the scenario lab lead the first-use experience with authentic PIX, BRL, and Brazilian banking context.
- **Do** keep the conversation familiar and put instrumentation directly beneath the output it explains.
- **Do** show skipped stages, blocked glyphs, unknown values, and full violation evidence explicitly.
- **Do** preserve the shared light/dark semantic token model, visible keyboard focus, reduced-motion behavior, and minimum touch targets.
- **Do** distinguish guided exploration from independent evaluation in the interface copy.

### Don't:

- **Don't** return to the obsolete purple identity, Portuguese interface chrome, or serif “approved text” metaphor.
- **Don't** make the interface look like a generic analytics dashboard or decorate it with unsourced charts, scores, balances, latency claims, or production-bank cues.
- **Don't** hide skipped gates, reduce blocked state to color, or collapse refusal evidence into a generic “blocked” label.
- **Don't** invent measurements for greetings and reopened threads; unknown is a dash or an omitted rail, never a confident zero.
- **Don't** auto-send scenario prompts or blur guided demonstration into evaluation results.
