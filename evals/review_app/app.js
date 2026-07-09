"use strict";

const VALID_CHOICES = new Set(["left", "right", "tie", "both_bad"]);
const VALID_FLAGS = new Set([
  "missing_fact",
  "too_terse",
  "too_verbose",
  "hard_to_scan",
  "needs_followup",
  "unsupported_claim",
]);

const TOKEN_STORAGE_KEY = "simple-man.blind-review.token";
const queryToken = new URLSearchParams(window.location.search).get("token")?.trim() || "";
let storedToken = "";
try {
  storedToken = window.sessionStorage.getItem(TOKEN_STORAGE_KEY)?.trim() || "";
} catch {
  // Session storage can be disabled; the query token still supports this page load.
}
const reviewToken = queryToken || storedToken;
if (queryToken) {
  try {
    window.sessionStorage.setItem(TOKEN_STORAGE_KEY, queryToken);
  } catch {
    // Keeping the token in memory is sufficient until the tab reloads.
  }
  const scrubbedUrl = new URL(window.location.href);
  scrubbedUrl.searchParams.delete("token");
  window.history.replaceState(null, document.title, `${scrubbedUrl.pathname}${scrubbedUrl.search}${scrubbedUrl.hash}`);
}

const elements = {
  app: document.querySelector("#app"),
  securityGate: document.querySelector("#security-gate"),
  loadingGate: document.querySelector("#loading-gate"),
  runId: document.querySelector("#run-id"),
  progressCount: document.querySelector("#progress-count"),
  positionCount: document.querySelector("#position-count"),
  progressTrack: document.querySelector("#progress-track"),
  statusText: document.querySelector("#status-text"),
  saveState: document.querySelector("#save-state"),
  blindStamp: document.querySelector("#blind-stamp"),
  reviewView: document.querySelector("#review-view"),
  resultsView: document.querySelector("#results-view"),
  caseNumber: document.querySelector("#case-number"),
  categoryTag: document.querySelector("#category-tag"),
  languageTag: document.querySelector("#language-tag"),
  ratedTag: document.querySelector("#rated-tag"),
  promptText: document.querySelector("#prompt-text"),
  contextPanel: document.querySelector("#context-panel"),
  contextText: document.querySelector("#context-text"),
  leftText: document.querySelector("#left-text"),
  rightText: document.querySelector("#right-text"),
  leftCard: document.querySelector("#left-card"),
  rightCard: document.querySelector("#right-card"),
  choiceButtons: Array.from(document.querySelectorAll("[data-choice]")),
  flagInputs: Array.from(document.querySelectorAll(".flags-fieldset input[type='checkbox']")),
  note: document.querySelector("#review-note"),
  previousButton: document.querySelector("#previous-button"),
  nextButton: document.querySelector("#next-button"),
  sealPanel: document.querySelector("#seal-panel"),
  sealButton: document.querySelector("#seal-button"),
  resultsSubtitle: document.querySelector("#results-subtitle"),
  resultsContent: document.querySelector("#results-content"),
  toast: document.querySelector("#toast"),
};

const session = {
  state: null,
  rating: emptyRating(),
  baselineRating: JSON.stringify(emptyRating()),
  dirty: false,
  busy: false,
  requestSequence: 0,
  toastTimer: null,
};

function emptyRating() {
  return { choice: null, flags: { left: [], right: [] }, note: "" };
}

function normalizeRating(value) {
  if (!value || typeof value !== "object") return emptyRating();

  const rawFlags = value.flags && typeof value.flags === "object" && !Array.isArray(value.flags)
    ? value.flags
    : {};
  const flagsFor = (side) => Array.isArray(rawFlags[side])
    ? rawFlags[side].filter((flag) => VALID_FLAGS.has(flag))
    : [];

  return {
    choice: VALID_CHOICES.has(value.choice) ? value.choice : null,
    flags: { left: flagsFor("left"), right: flagsFor("right") },
    note: typeof value.note === "string" ? value.note : "",
  };
}

function canonicalRating(rating) {
  return JSON.stringify({
    choice: rating.choice,
    flags: {
      left: [...rating.flags.left].sort(),
      right: [...rating.flags.right].sort(),
    },
    note: rating.note.trim(),
  });
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Review-Token", reviewToken);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");

  const response = await fetch(path, {
    ...options,
    headers,
    cache: "no-store",
    credentials: "same-origin",
  });

  const rawBody = await response.text();
  let body = null;
  if (rawBody) {
    try {
      body = JSON.parse(rawBody);
    } catch {
      body = rawBody;
    }
  }

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      try {
        window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
      } catch {
        // Nothing else to clear.
      }
    }
    const detail = body && typeof body === "object" ? body.detail || body.error : body;
    throw new Error(detail || `Request failed (${response.status})`);
  }

  return body;
}

function showFatal(title, detail) {
  elements.loadingGate.classList.add("is-hidden");
  elements.app.classList.add("is-hidden");
  elements.securityGate.classList.remove("is-hidden");
  elements.securityGate.querySelector("h1").textContent = title;
  const paragraphs = elements.securityGate.querySelectorAll("p");
  if (paragraphs[1]) paragraphs[1].textContent = detail;
  if (paragraphs[2]) paragraphs[2].textContent = "Fix the local session, then reload this page.";
}

function setBusy(isBusy, message = "") {
  session.busy = isBusy;
  document.body.classList.toggle("is-busy", isBusy);
  elements.previousButton.disabled = isBusy || !session.state || session.state.current_index <= 0;
  elements.nextButton.disabled = isBusy || !session.rating.choice;
  elements.sealButton.disabled = isBusy;
  if (message) elements.saveState.textContent = message;
  if (!isBusy && session.state && !session.state.sealed) updateDirtyState();
}

function showToast(message, kind = "info") {
  window.clearTimeout(session.toastTimer);
  elements.toast.textContent = message;
  elements.toast.dataset.kind = kind;
  elements.toast.classList.add("is-visible");
  session.toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 3200);
}

function formatPlainText(value) {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value, null, 2);
}

function ratingFromState(state) {
  if (state.rating !== undefined) return normalizeRating(state.rating);

  const ratings = state.ratings;
  const pairId = state.pair?.id;

  if (Array.isArray(ratings)) {
    const found = ratings.find((rating) => rating.pair_id === pairId || rating.id === pairId);
    return normalizeRating(found?.rating || found);
  }

  if (ratings && typeof ratings === "object") {
    if (VALID_CHOICES.has(ratings.choice)) return normalizeRating(ratings);
    const found = ratings[pairId] || ratings.current;
    return normalizeRating(found?.rating || found);
  }

  return emptyRating();
}

function completedCount(state) {
  if (Number.isFinite(state.rated_count)) return Math.max(0, state.rated_count);
  if (Number.isFinite(state.completed)) return Math.max(0, state.completed);
  if (state.completed === true) return state.total;
  if (Array.isArray(state.ratings)) return state.ratings.length;
  if (state.ratings && typeof state.ratings === "object") {
    return VALID_CHOICES.has(state.ratings.choice) ? 1 : Object.keys(state.ratings).length;
  }
  return 0;
}

function updateProgress(state) {
  const total = Math.max(0, Number(state.total) || 0);
  const completed = Math.min(total, completedCount(state));
  const position = total ? Math.min(total, Number(state.current_index) + 1) : 0;
  elements.runId.textContent = state.run_id || "—";
  elements.runId.title = state.run_id || "";
  elements.progressCount.textContent = `${completed} / ${total} rated`;
  elements.positionCount.textContent = `Pair ${position} / ${total}`;
  elements.progressTrack.max = total || 1;
  elements.progressTrack.value = completed;
}

function renderForm() {
  for (const button of elements.choiceButtons) {
    const selected = button.dataset.choice === session.rating.choice;
    button.setAttribute("aria-pressed", String(selected));
    button.classList.toggle("is-selected", selected);
  }

  elements.leftCard.classList.toggle("is-selected", session.rating.choice === "left");
  elements.rightCard.classList.toggle("is-selected", session.rating.choice === "right");

  for (const input of elements.flagInputs) {
    input.checked = session.rating.flags[input.dataset.side].includes(input.value);
  }
  elements.note.value = session.rating.note;

  elements.nextButton.disabled = session.busy || !session.rating.choice;
  elements.ratedTag.textContent = session.rating.choice ? "Rated" : "Unrated";
  elements.ratedTag.classList.toggle("is-rated", Boolean(session.rating.choice));
  updateDirtyState();
}

function updateDirtyState() {
  session.dirty = canonicalRating(session.rating) !== session.baselineRating;
  if (session.busy) return;

  if (session.dirty) {
    elements.saveState.textContent = "Unsaved changes";
    elements.saveState.classList.add("is-dirty");
  } else {
    elements.saveState.textContent = session.rating.choice ? "Rating saved" : "No rating yet";
    elements.saveState.classList.remove("is-dirty");
  }
}

function renderReviewState(state) {
  if (!state.pair) {
    showFatal("No review pair available", "The server returned an unsealed run without a current pair.");
    return;
  }

  session.state = state;
  session.rating = ratingFromState(state);
  session.baselineRating = canonicalRating(session.rating);
  session.dirty = false;

  elements.loadingGate.classList.add("is-hidden");
  elements.securityGate.classList.add("is-hidden");
  elements.app.classList.remove("is-hidden");
  elements.reviewView.classList.remove("is-hidden");
  elements.resultsView.classList.add("is-hidden");
  elements.blindStamp.classList.remove("is-revealed");
  elements.blindStamp.innerHTML = '<span class="stamp-dot" aria-hidden="true"></span>Arms concealed';

  updateProgress(state);

  const pair = state.pair;
  const currentIndex = Number(state.current_index) || 0;
  const total = Number(state.total) || 0;
  const completed = completedCount(state);

  elements.caseNumber.textContent = String(currentIndex + 1).padStart(2, "0");
  elements.categoryTag.textContent = pair.category || "Uncategorized";
  elements.languageTag.textContent = pair.language || "—";
  elements.promptText.textContent = formatPlainText(pair.prompt);
  elements.leftText.textContent = formatPlainText(pair.left?.text);
  elements.rightText.textContent = formatPlainText(pair.right?.text);

  if (pair.verified_context !== undefined && pair.verified_context !== null && pair.verified_context !== "") {
    elements.contextText.textContent = formatPlainText(pair.verified_context);
    elements.contextPanel.classList.remove("is-hidden");
  } else {
    elements.contextText.textContent = "";
    elements.contextPanel.classList.add("is-hidden");
    elements.contextPanel.open = false;
  }

  elements.previousButton.disabled = session.busy || currentIndex <= 0;
  elements.nextButton.innerHTML = currentIndex < total - 1
    ? 'Save + next <span aria-hidden="true">→</span>'
    : 'Save rating <span aria-hidden="true">✓</span>';

  const readyToSeal = total > 0 && completed >= total;
  elements.sealPanel.classList.toggle("is-hidden", !readyToSeal);
  elements.statusText.textContent = session.rating.choice
    ? `Editing saved verdict for case ${currentIndex + 1}`
    : `Case ${currentIndex + 1} awaits a verdict`;

  renderForm();
  document.title = `${currentIndex + 1}/${total} · Blind Review`;
}

async function loadState(index) {
  const sequence = ++session.requestSequence;
  setBusy(true, "Loading case…");

  try {
    const suffix = Number.isInteger(index) ? `?index=${encodeURIComponent(index)}` : "";
    const state = await api(`/api/state${suffix}`);
    if (sequence !== session.requestSequence) return;

    if (state.sealed) {
      session.state = state;
      updateProgress(state);
      await loadResults();
    } else {
      renderReviewState(state);
    }
  } catch (error) {
    if (!session.state) showFatal("Unable to load review", error.message);
    else showToast(error.message, "error");
  } finally {
    if (sequence === session.requestSequence) setBusy(false);
  }
}

function choose(choice) {
  if (session.busy || session.state?.sealed || !VALID_CHOICES.has(choice)) return;
  session.rating.choice = choice;
  renderForm();
  elements.statusText.textContent = `Verdict selected: ${choiceLabel(choice)}`;
}

function choiceLabel(choice) {
  return {
    left: "A better",
    right: "B better",
    tie: "Tie",
    both_bad: "Both bad",
  }[choice] || choice;
}

async function saveRating({ advance = false } = {}) {
  if (session.busy) return false;
  if (!session.rating.choice) {
    showToast("Choose A, B, tie, or both bad first.", "error");
    document.querySelector(".choice-row button")?.focus();
    return false;
  }

  const currentIndex = Number(session.state.current_index) || 0;
  const total = Number(session.state.total) || 0;

  if (session.dirty) {
    setBusy(true, "Saving verdict…");
    try {
      const payload = {
        choice: session.rating.choice,
        flags: {
          left: [...session.rating.flags.left],
          right: [...session.rating.flags.right],
        },
        note: session.rating.note.trim(),
      };
      await api(`/api/ratings/${encodeURIComponent(session.state.pair.id)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      session.baselineRating = canonicalRating(payload);
      session.rating = normalizeRating(payload);
      session.dirty = false;
      showToast("Verdict saved.", "success");
    } catch (error) {
      showToast(error.message, "error");
      setBusy(false);
      updateDirtyState();
      return false;
    }
  }

  const targetIndex = advance && currentIndex < total - 1 ? currentIndex + 1 : currentIndex;
  await loadState(targetIndex);
  return true;
}

async function navigateTo(index) {
  if (session.busy || !Number.isInteger(index)) return;

  if (session.dirty) {
    if (!session.rating.choice) {
      showToast("Choose a verdict to save these notes before leaving.", "error");
      return;
    }
    const saved = await saveRating({ advance: false });
    if (!saved) return;
  }

  if (session.state?.current_index !== index) await loadState(index);
}

async function sealReview() {
  if (session.busy) return;
  const total = Number(session.state?.total) || 0;
  if (completedCount(session.state) < total) {
    showToast("Every pair needs a saved verdict before sealing.", "error");
    return;
  }

  const confirmed = window.confirm(
    "Seal this review and reveal the benchmark arms? Ratings cannot be edited after sealing.",
  );
  if (!confirmed) return;

  setBusy(true, "Sealing review…");
  try {
    await api("/api/seal", { method: "POST" });
    await loadState();
  } catch (error) {
    showToast(error.message, "error");
    setBusy(false);
  }
}

function makeElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function humanize(value) {
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function renderResultsSummary(results) {
  const summary = makeElement("section", "result-summary");
  summary.setAttribute("aria-label", "Result summary");

  const totalCard = makeElement("article", "metric-card metric-card-total");
  totalCard.append(makeElement("span", "metric-label", "Pairs judged"));
  totalCard.append(makeElement("strong", "metric-value", String(results.total ?? results.pairs?.length ?? 0)));
  totalCard.append(makeElement("small", "metric-detail", `Sealed ${results.sealed_at || "—"}`));
  summary.append(totalCard);

  const wins = results.wins && typeof results.wins === "object" ? Object.entries(results.wins) : [];
  for (const [arm, count] of wins) {
    const card = makeElement("article", "metric-card");
    card.append(makeElement("span", "metric-label", humanize(arm)));
    card.append(makeElement("strong", "metric-value", String(count)));
    card.append(makeElement("small", "metric-detail", "wins"));
    summary.append(card);
  }

  const verdictCounts = results.verdict_counts && typeof results.verdict_counts === "object"
    ? results.verdict_counts
    : {};
  for (const verdict of ["tie", "both_bad"]) {
    const count = Number(verdictCounts[verdict]) || 0;
    if (!count) continue;
    const card = makeElement("article", "metric-card metric-card-verdict");
    card.append(makeElement("span", "metric-label", choiceLabel(verdict)));
    card.append(makeElement("strong", "metric-value", String(count)));
    card.append(makeElement("small", "metric-detail", "verdicts"));
    summary.append(card);
  }

  return summary;
}

function renderFlagCounts(flagCounts) {
  const section = makeElement("section", "flag-results result-block");
  const header = makeElement("div", "result-block-header");
  header.append(makeElement("p", "eyebrow", "Review signals"));
  header.append(makeElement("h3", "", "Flags by revealed arm"));
  section.append(header);

  const list = makeElement("div", "arm-flag-count-grid");
  const rawEntries = flagCounts && typeof flagCounts === "object" ? Object.entries(flagCounts) : [];
  const nestedEntries = rawEntries.filter(([, counts]) => counts && typeof counts === "object");
  if (!nestedEntries.length) {
    list.append(makeElement("p", "empty-result", "No answer-specific flags recorded."));
    section.append(list);
    return section;
  }

  const max = Math.max(
    1,
    ...nestedEntries.flatMap(([, counts]) => Object.values(counts).map((count) => Number(count) || 0)),
  );
  for (const [arm, counts] of nestedEntries) {
    const armBlock = makeElement("article", "arm-flag-counts");
    armBlock.append(makeElement("h4", "", humanize(arm)));
    const entries = Object.entries(counts);
    if (!entries.length) armBlock.append(makeElement("p", "empty-result", "No flags"));

    for (const [flag, count] of entries) {
      const item = makeElement("div", "flag-count");
      const copy = makeElement("div", "flag-count-copy");
      copy.append(makeElement("span", "", humanize(flag)));
      copy.append(makeElement("strong", "", String(count)));
      const track = makeElement("progress", "flag-count-track");
      track.max = max;
      track.value = Number(count) || 0;
      track.setAttribute("aria-label", `${humanize(arm)}, ${humanize(flag)}: ${count}`);
      item.append(copy, track);
      armBlock.append(item);
    }
    list.append(armBlock);
  }
  section.append(list);
  return section;
}

function renderPairResults(pairs) {
  const section = makeElement("section", "pair-results result-block");
  const header = makeElement("div", "result-block-header");
  header.append(makeElement("p", "eyebrow", "Pair-level reveal"));
  header.append(makeElement("h3", "", "What A and B actually were"));
  section.append(header);

  const list = makeElement("div", "reveal-list");
  if (!Array.isArray(pairs) || !pairs.length) {
    list.append(makeElement("p", "empty-result", "No pair results returned."));
    section.append(list);
    return section;
  }

  pairs.forEach((pair, index) => {
    const article = makeElement("article", "reveal-row");
    const number = makeElement("div", "reveal-number", String(index + 1).padStart(2, "0"));
    const identity = makeElement("div", "reveal-identity");
    identity.append(makeElement("span", "task-id", pair.task_id || pair.pair_id || `Pair ${index + 1}`));

    const mapping = makeElement("div", "arm-mapping");
    const left = makeElement("span");
    left.append(makeElement("b", "", "A"), document.createTextNode(` ${pair.left_arm ?? "—"}`));
    const right = makeElement("span");
    right.append(makeElement("b", "", "B"), document.createTextNode(` ${pair.right_arm ?? "—"}`));
    mapping.append(left, right);
    identity.append(mapping);

    const verdict = makeElement("div", "reveal-verdict");
    verdict.append(makeElement("span", "meta-label", "Your verdict"));
    verdict.append(makeElement("strong", "", choiceLabel(pair.choice)));
    verdict.append(makeElement("small", "", `Winner: ${pair.winner ?? "—"}`));

    const notes = makeElement("div", "reveal-notes");
    let flagGroups = [];
    if (pair.arm_flags && typeof pair.arm_flags === "object") {
      flagGroups = Object.entries(pair.arm_flags);
    } else if (pair.flags && typeof pair.flags === "object" && !Array.isArray(pair.flags)) {
      flagGroups = [
        [pair.left_arm || "Answer A", pair.flags.left || []],
        [pair.right_arm || "Answer B", pair.flags.right || []],
      ];
    } else if (Array.isArray(pair.flags)) {
      flagGroups = [["Flags", pair.flags]];
    }

    const populatedFlagGroups = flagGroups.filter(([, flags]) => Array.isArray(flags) && flags.length);
    for (const [arm, flags] of populatedFlagGroups) {
      const group = makeElement("div", "mini-flag-group");
      group.append(makeElement("strong", "", humanize(arm)));
      const flagList = makeElement("div", "mini-flags");
      flags.forEach((flag) => flagList.append(makeElement("span", "", humanize(flag))));
      group.append(flagList);
      notes.append(group);
    }
    if (pair.note) notes.append(makeElement("p", "", pair.note));
    if (!populatedFlagGroups.length && !pair.note) {
      notes.append(makeElement("span", "no-note", "No flags or note"));
    }

    article.append(number, identity, verdict, notes);
    list.append(article);
  });
  section.append(list);
  return section;
}

async function loadResults() {
  try {
    const results = await api("/api/results");
    elements.loadingGate.classList.add("is-hidden");
    elements.securityGate.classList.add("is-hidden");
    elements.app.classList.remove("is-hidden");
    elements.reviewView.classList.add("is-hidden");
    elements.resultsView.classList.remove("is-hidden");
    elements.blindStamp.classList.add("is-revealed");
    elements.blindStamp.textContent = "Arms revealed";
    elements.resultsSubtitle.textContent = `Final benchmark results · ${results.run_id || session.state?.run_id || "local run"}`;
    elements.resultsContent.replaceChildren(
      renderResultsSummary(results),
      renderFlagCounts(results.flag_counts),
      renderPairResults(results.pairs),
    );
    elements.statusText.textContent = "Review sealed. Ratings are read-only.";
    elements.saveState.textContent = "Final";
    document.title = "Results · Blind Review";
  } catch (error) {
    showFatal("Unable to reveal results", error.message);
  }
}

function wireEvents() {
  for (const button of elements.choiceButtons) {
    button.addEventListener("click", () => choose(button.dataset.choice));
  }

  for (const input of elements.flagInputs) {
    input.addEventListener("change", () => {
      session.rating.flags = {
        left: elements.flagInputs
          .filter((item) => item.dataset.side === "left" && item.checked)
          .map((item) => item.value),
        right: elements.flagInputs
          .filter((item) => item.dataset.side === "right" && item.checked)
          .map((item) => item.value),
      };
      updateDirtyState();
    });
  }

  elements.note.addEventListener("input", () => {
    session.rating.note = elements.note.value;
    updateDirtyState();
  });

  elements.previousButton.addEventListener("click", () => navigateTo(session.state.current_index - 1));
  elements.nextButton.addEventListener("click", () => saveRating({ advance: true }));
  elements.sealButton.addEventListener("click", sealReview);

  document.addEventListener("keydown", (event) => {
    if (session.state?.sealed || session.busy) return;

    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      saveRating({ advance: true });
      return;
    }

    const tagName = event.target?.tagName?.toLowerCase();
    if (["input", "textarea", "select"].includes(tagName) || event.metaKey || event.ctrlKey || event.altKey) return;

    const choice = { a: "left", b: "right", t: "tie", x: "both_bad" }[event.key.toLowerCase()];
    if (choice) {
      event.preventDefault();
      choose(choice);
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (!session.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function boot() {
  if (!reviewToken) {
    elements.loadingGate.classList.add("is-hidden");
    elements.securityGate.classList.remove("is-hidden");
    return;
  }

  wireEvents();
  loadState();
}

boot();
