# Demo5 screenshot observation fix

## Confirmed cause and scope

The preserved prototype `35cdeb52f296...`, source `91f0bbb91fff...`, completed
all 19 actions on the failing field-errors route. `capture_page` then used
Playwright's default caret hiding. Real Chromium 153.0.8010.12 changed five
iframe input/textarea `style` attributes to `caret-color: transparent !important`,
then restored them as empty attributes. The prototype's MutationObserver
correctly detected markup changes and invalidated its completed measurement.
Its new diagnostic and the captured screenshot identify `application.markup`.
The exact candidate/action replay reproduces the failure; passing only
`caret="initial"` to both screenshots preserves the measured state, produces
all tiles, and records no such mutations. This is a controller capture side
effect, not proof that the entire prototype is acceptable.

The shared helper is used by static prototype and implemented-application
inspection. Only its initial and tile screenshot calls need adjustment.
Existing visibility/text observations and the terminal checks before/after
tiles must remain unchanged. Do not modify the product to ignore DOM changes.

## Design and compatibility

Explicitly use the supported `caret="initial"` option for both captures. Keep
viewport-only capture, scrolling/restoration, geometry limits, and all existing
error/cancellation propagation. The actual caret may appear in an image;
screenshots are real observations, not normalized pixel comparison fixtures.
Do not add CSS, strip attributes, suppress observers, delay for arbitrary time,
or remove terminal assertions. This avoids aa2's known DOM edit; it does not
promise zero browser rendering side effects or freeze an application's own
asynchronous state.

Bump the common browser protocol from 11 to 12. Existing automatic approval,
application, and pending-delivery version bindings must recheck old evidence;
bound human consent and completed-run history are preserved through existing
mechanisms. Do not rewrite the live run or old records.

## Validation

1. Preserve and replay the exact Demo5 source/actions with the cached browser;
   compare original default versus explicit initial caret, retain JSON/PNG.
2. Real prototype/application public entries with top-level and iframe input
   mutation observers: initial image and all below-fold tiles preserve markup,
   field value, focus, and storage. Include existing inline caret styles.
3. Negative browser case: a real state change during capture still fails the
   unchanged terminal assertion. A previously failed action remains failed and
   its diagnostic image must not mutate the inputs either.
4. Old version 11 evidence rechecks while retaining the original human binding;
   existing completion-pending identity, hidden text, native buttons, SVG,
   navigation, script error, geometry, and cancellation regressions continue.
5. Targeted subsystem tests first, full fixed-source suite and independent review
   before normal same-run recovery. No product acceptance from this replay.

## Non-goals, risk, rollback

No new browser action, generic test framework, retry budget, capture-mode
configuration, product change, permission change, or provider fix. The current
decision ProviderError is a distinct unavailable-call fallback, not evidence
that prototype acceptance was denied on semantics. Its original pending
question remains bound and unanswered during maintenance.

Risk is visible caret variation in images; actual state preservation is the
intended tradeoff. If the option causes a regression, revert the implementation
through Git with another protocol increment; do not restore acceptance of old
automatic evidence or edit run state. Both the current failure and new replay
materials stay available for review.
