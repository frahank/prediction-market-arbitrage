/*
 * SPDX-FileCopyrightText: 2026 Farhan M Khan <https://farhank.dev>
 * SPDX-License-Identifier: MIT
 */
(function () {
  var workspace = document.querySelector("[data-docs-workspace]");
  if (!workspace) {
    return;
  }

  var docList = workspace.querySelector("[data-doc-list]");
  var noteList = workspace.querySelector("[data-note-list]");
  var readerTitle = workspace.querySelector("[data-reader-title]");
  var readerKicker = workspace.querySelector("[data-reader-kicker]");
  var renderedDoc = workspace.querySelector("[data-doc-rendered]");
  var docStatus = workspace.querySelector("[data-doc-status]");
  var noteStatus = workspace.querySelector("[data-note-status]");
  var noteTitle = workspace.querySelector("[data-note-title]");
  var noteMarkdown = workspace.querySelector("[data-note-markdown]");
  var saveNoteButton = workspace.querySelector("[data-save-note]");
  var newNoteForm = workspace.querySelector("[data-new-note-form]");
  var currentNote = null;

  function setStatus(node, text, isError) {
    if (!node) {
      return;
    }
    node.textContent = text || "";
    node.classList.toggle("is-error", Boolean(isError));
  }

  async function callApi(path, options) {
    var response = await fetch(path, options || { headers: { "accept": "application/json" } });
    var payload = await response.json();
    if (!payload || payload.ok !== true) {
      var err = payload && payload.error ? payload.error : { code: "error", message: "operation failed" };
      throw err;
    }
    return payload.data;
  }

  function docGroup(path) {
    if (path === "README.md") {
      return "README";
    }
    if (path.indexOf("docs/notes/") === 0) {
      return "notes/";
    }
    if (path.indexOf("docs/") === 0) {
      return "docs/";
    }
    return "other";
  }

  function button(label, className) {
    var item = document.createElement("button");
    item.type = "button";
    item.className = className;
    item.textContent = label;
    return item;
  }

  function renderDocList(docs) {
    docList.textContent = "";
    var groups = { "README": [], "docs/": [], "notes/": [], "other": [] };
    docs.forEach(function (doc) {
      groups[docGroup(doc.path)].push(doc);
    });
    Object.keys(groups).forEach(function (group) {
      if (!groups[group].length) {
        return;
      }
      var heading = document.createElement("h3");
      heading.textContent = group;
      docList.appendChild(heading);
      groups[group].forEach(function (doc) {
        var item = button(doc.title || doc.path, "list-row");
        item.title = doc.path;
        item.addEventListener("click", function () {
          loadDoc(doc.path);
        });
        docList.appendChild(item);
      });
    });
  }

  function renderNoteList(notes) {
    noteList.textContent = "";
    if (!notes.length) {
      var empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No notes yet.";
      noteList.appendChild(empty);
      return;
    }
    notes.forEach(function (note) {
      var item = button(note.name + " v" + note.version, "list-row");
      item.addEventListener("click", function () {
        loadNote(note.name);
      });
      noteList.appendChild(item);
    });
  }

  async function loadDocs() {
    try {
      renderDocList(await callApi("/api/list_docs"));
    } catch (err) {
      setStatus(docStatus, err.message || err.code, true);
    }
  }

  async function loadNotes() {
    try {
      renderNoteList(await callApi("/api/list_notes"));
    } catch (err) {
      setStatus(noteStatus, err.message || err.code, true);
    }
  }

  async function loadDoc(path) {
    setStatus(docStatus, "Loading...", false);
    try {
      var doc = await callApi("/api/read_doc?path=" + encodeURIComponent(path));
      readerKicker.textContent = doc.path;
      readerTitle.textContent = doc.title || doc.path;
      renderedDoc.innerHTML = doc.rendered_html || "";
      setStatus(docStatus, "", false);
    } catch (err) {
      setStatus(docStatus, err.message || err.code, true);
    }
  }

  // A link between two repository documents is rendered as "#doc=<path>". The
  // page is a single view, so open the target in this reader instead of letting
  // the browser navigate to a fragment that means nothing to it.
  renderedDoc.addEventListener("click", function (event) {
    var anchor = event.target.closest ? event.target.closest("a[href^='#doc=']") : null;
    if (!anchor) {
      return;
    }
    event.preventDefault();
    var target = decodeURIComponent(anchor.getAttribute("href").slice("#doc=".length));
    if (target) {
      loadDoc(target);
    }
  });

  async function loadNote(name) {
    setStatus(noteStatus, "Loading...", false);
    try {
      var note = await callApi("/api/read_note?name=" + encodeURIComponent(name));
      currentNote = { name: note.name, version: note.version };
      noteTitle.textContent = note.name + " v" + note.version;
      noteMarkdown.value = note.markdown || "";
      saveNoteButton.disabled = false;
      setStatus(noteStatus, "", false);
    } catch (err) {
      setStatus(noteStatus, err.message || err.code, true);
    }
  }

  async function saveCurrentNote() {
    if (!currentNote) {
      return;
    }
    setStatus(noteStatus, "Saving...", false);
    try {
      var saved = await callApi("/api/save_note", {
        method: "POST",
        headers: { "accept": "application/json", "content-type": "application/json" },
        body: JSON.stringify({
          name: currentNote.name,
          markdown: noteMarkdown.value,
          expected_version: currentNote.version
        })
      });
      currentNote.version = saved.version;
      noteTitle.textContent = currentNote.name + " v" + currentNote.version;
      setStatus(noteStatus, "Saved", false);
      await loadNotes();
      await loadDocs();
    } catch (err) {
      if (err.code === "conflict") {
        setStatus(noteStatus, "note changed on disk - reload", true);
      } else {
        setStatus(noteStatus, err.message || err.code, true);
      }
    }
  }

  async function createNote(event) {
    event.preventDefault();
    var input = newNoteForm.elements.name;
    var name = input.value.trim();
    if (!name) {
      setStatus(noteStatus, "Enter a note name.", true);
      return;
    }
    setStatus(noteStatus, "Creating...", false);
    try {
      await callApi("/api/save_note", {
        method: "POST",
        headers: { "accept": "application/json", "content-type": "application/json" },
        body: JSON.stringify({ name: name, markdown: "", expected_version: null })
      });
      input.value = "";
      await loadNotes();
      await loadDocs();
      await loadNote(name);
      noteMarkdown.focus();
    } catch (err) {
      setStatus(noteStatus, err.message || err.code, true);
    }
  }

  saveNoteButton.addEventListener("click", saveCurrentNote);
  newNoteForm.addEventListener("submit", createNote);
  loadDocs();
  loadNotes();
  var initialPath = new URLSearchParams(window.location.search).get("path");
  if (initialPath) {
    loadDoc(initialPath);
  }
}());
