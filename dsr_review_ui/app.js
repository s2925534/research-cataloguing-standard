(() => {
  const progressEl = document.getElementById("progress");
  const cardEl = document.getElementById("card");
  const emptyEl = document.getElementById("empty-state");
  const editPanel = document.getElementById("edit-panel");
  const editSubtypeRow = document.getElementById("edit-subtype-row");
  const editClassSelect = document.getElementById("edit-class");
  const editSubtypeSelect = document.getElementById("edit-subtype");

  let records = [];
  let totalAtStart = null;
  let vocab = { main_classes: [], artefact_subtypes: {} };
  let current = null;

  async function loadVocab() {
    const res = await fetch("/api/vocab");
    vocab = await res.json();
    editClassSelect.innerHTML = vocab.main_classes
      .map((c) => `<option value="${c}">${c}</option>`).join("");
    editClassSelect.addEventListener("change", renderSubtypeOptions);
  }

  function renderSubtypeOptions() {
    const isArt = editClassSelect.value === "ART";
    editSubtypeRow.classList.toggle("hidden", !isArt);
    if (isArt) {
      const subtypes = Object.keys(vocab.artefact_subtypes);
      editSubtypeSelect.innerHTML = subtypes
        .map((s) => `<option value="${s}">${s} — ${vocab.artefact_subtypes[s]}</option>`).join("");
    }
  }

  async function loadRecords() {
    const res = await fetch("/api/records");
    const data = await res.json();
    records = data.records;
    if (totalAtStart === null) totalAtStart = data.total_remaining;
    renderCurrent();
  }

  function renderCurrent() {
    if (records.length === 0) {
      cardEl.classList.add("hidden");
      emptyEl.classList.remove("hidden");
      progressEl.textContent = "0 remaining";
      return;
    }
    emptyEl.classList.add("hidden");
    cardEl.classList.remove("hidden");
    editPanel.classList.add("hidden");
    current = records[0];

    progressEl.textContent = `${records.length} remaining (of ${totalAtStart} flagged Requires Review)`;
    document.getElementById("class-badge").textContent = `${current.class_code}/${current.subtype_code}`;
    document.getElementById("stable-id").textContent = current.stable_id;
    document.getElementById("legacy-ids").textContent = current.legacy_ids.join(", ");
    document.getElementById("file-name").textContent = current.file_name;
    document.getElementById("source-path").textContent = current.source_path;
    document.getElementById("open-file").href = "file://" + encodeURI(current.source_path);
    document.getElementById("classification-rule").textContent = current.classification_rule;
    document.getElementById("classification-evidence").textContent = current.classification_evidence;
    document.getElementById("current-class").textContent = current.class_code;
    document.getElementById("current-subtype").textContent = current.subtype_code;
    document.getElementById("current-version").textContent = current.version;

    editClassSelect.value = current.class_code;
    renderSubtypeOptions();
    if (current.class_code === "ART") editSubtypeSelect.value = current.subtype_code;
  }

  async function decide(action, extra) {
    if (!current) return;
    const payload = Object.assign({ catalogue_id: current.catalogue_id, action }, extra || {});
    const res = await fetch("/api/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(err.error || "Failed to save decision");
      return;
    }
    records.shift();
    renderCurrent();
  }

  document.getElementById("btn-approve").addEventListener("click", () => decide("approve"));
  document.getElementById("btn-skip").addEventListener("click", () => decide("skip"));
  document.getElementById("btn-edit-toggle").addEventListener("click", () => {
    editPanel.classList.toggle("hidden");
  });
  document.getElementById("btn-save-edit").addEventListener("click", () => {
    const class_code = editClassSelect.value;
    // Non-ART classes have no subtype vocabulary exposed here - omit the
    // field entirely so the backend preserves whatever subtype_code the
    // record already had instead of overwriting it with a placeholder.
    const extra = { class_code };
    if (class_code === "ART") extra.subtype_code = editSubtypeSelect.value;
    decide("edit", extra);
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "SELECT") return;
    if (e.key === "a") decide("approve");
    if (e.key === "s") decide("skip");
    if (e.key === "e") editPanel.classList.toggle("hidden");
  });

  loadVocab().then(loadRecords);
})();
