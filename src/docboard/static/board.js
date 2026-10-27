// docboard: drag a card onto a column to move it. Every card's "Move" control does the same
// without this script. The POST carries the page's own Origin, which the server checks.
"use strict";
(() => {
  let dragged = null;
  for (const card of document.querySelectorAll("[data-card-id]")) {
    card.addEventListener("dragstart", (e) => {
      dragged = card;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.cardId);
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      dragged = null;
    });
  }
  for (const column of document.querySelectorAll("[data-column]")) {
    column.addEventListener("dragover", (e) => {
      if (dragged) {
        e.preventDefault();
        column.classList.add("over");
      }
    });
    column.addEventListener("dragleave", () => column.classList.remove("over"));
    column.addEventListener("drop", async (e) => {
      e.preventDefault();
      column.classList.remove("over");
      if (!dragged || dragged.closest("[data-column]") === column) return;
      const body = new URLSearchParams({ col: column.dataset.column });
      try {
        await fetch(dragged.dataset.moveUrl, {
          method: "POST",
          body,
          credentials: "same-origin",
          redirect: "manual",
        });
      } finally {
        location.reload();
      }
    });
  }
})();
