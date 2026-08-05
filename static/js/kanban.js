document.addEventListener("DOMContentLoaded", () => {
  const board = document.querySelector("[data-kanban]");
  if (!board) return;

  const feedback = board.querySelector(".kanban-feedback");
  const noteDialog = board.querySelector(".kanban-note-dialog");
  const noteInput = board.querySelector(".kanban-note-input");
  const noteTitle = board.querySelector(".kanban-note-title");
  const bulkApply = board.querySelector(".kanban-bulk-apply");
  const statusesRequiringNote = new Set([
    "Bloqueado",
    "Resolvido",
    "Cancelado",
    "Reaberto",
  ]);
  let draggedCard = null;

  const updateColumnState = () => {
    board.querySelectorAll(".kanban-column").forEach((column) => {
      const cards = column.querySelectorAll(".kanban-card");
      column.querySelector(".kanban-count").textContent = cards.length;
      column.querySelector(".kanban-empty").classList.toggle("hidden", cards.length > 0);
    });
  };

  const showFeedback = (message, success) => {
    feedback.textContent = message;
    feedback.className = `kanban-feedback rounded-2xl px-4 py-3 text-sm font-bold ${
      success ? "bg-emerald-50 text-emerald-800" : "bg-red-50 text-red-800"
    }`;
    feedback.focus?.();
  };

  const responseError = async (response) => {
    try {
      const data = await response.json();
      return data.detail || "Não foi possível atualizar o status.";
    } catch (error) {
      return "Não foi possível atualizar o status.";
    }
  };

  const requestTransitionNote = (card, status) => {
    if (!statusesRequiringNote.has(status)) return Promise.resolve(null);
    if (!noteDialog?.showModal) {
      const note = window.prompt(`Informe o motivo para mover a demanda para ${status}:`);
      return Promise.resolve(note?.trim().length >= 5 ? note.trim() : false);
    }

    noteTitle.textContent = `Mover #${card.dataset.ticketId} para ${status}`;
    noteInput.value = "";
    noteDialog.returnValue = "cancel";
    noteDialog.showModal();
    noteInput.focus();
    return new Promise((resolve) => {
      noteDialog.addEventListener("close", () => {
        if (noteDialog.returnValue !== "confirm") {
          resolve(false);
          return;
        }
        resolve(noteInput.value.trim());
      }, { once: true });
    });
  };

  const refreshStatusOptions = async (card, select) => {
    try {
      const response = await fetch(`/api/tickets/${card.dataset.ticketId}/state`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return;
      const state = await response.json();
      const options = [state.status, ...(state.allowed_statuses || [])];
      select.replaceChildren(...options.map((value) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        option.selected = value === state.status;
        return option;
      }));
    } catch (error) {
      // A transição já foi confirmada; a próxima atualização de página recompõe as opções.
    }
  };

  const moveTicket = async (card, status) => {
    const previousStatus = card.dataset.currentStatus;
    if (status === previousStatus) return;
    const previousColumn = card.closest(".kanban-column");
    const nextColumn = board.querySelector(`.kanban-column[data-status="${CSS.escape(status)}"]`);
    const select = card.querySelector(".kanban-status");
    const note = await requestTransitionNote(card, status);
    if (note === false) {
      select.value = previousStatus;
      showFeedback("Alteração cancelada. O status não foi modificado.", false);
      return;
    }

    card.classList.add("opacity-60", "pointer-events-none");
    if (nextColumn) {
      nextColumn.querySelector("[data-dropzone]").prepend(card);
    }
    updateColumnState();

    try {
      const response = await fetch(`/api/tickets/${card.dataset.ticketId}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, note }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      card.dataset.currentStatus = status;
      select.value = status;
      if (!nextColumn) {
        card.remove();
        updateColumnState();
      } else {
        await refreshStatusOptions(card, select);
      }
      showFeedback(`Demanda #${card.dataset.ticketId} movida para ${status}.`, true);
    } catch (error) {
      if (card.isConnected) {
        previousColumn.querySelector("[data-dropzone]").prepend(card);
      }
      select.value = previousStatus;
      updateColumnState();
      showFeedback(error.message || "Não foi possível atualizar o status.", false);
    } finally {
      card.classList.remove("opacity-60", "pointer-events-none");
    }
  };

  board.querySelectorAll(".kanban-card").forEach((card) => {
    card.addEventListener("dragstart", () => {
      draggedCard = card;
      card.classList.add("is-dragging");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("is-dragging");
      draggedCard = null;
      board.querySelectorAll(".kanban-dropzone").forEach((zone) => zone.classList.remove("is-over"));
    });
    card.querySelector(".kanban-status").addEventListener("change", (event) => {
      moveTicket(card, event.target.value);
    });
  });

  board.querySelectorAll("[data-dropzone]").forEach((zone) => {
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("is-over");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("is-over");
      if (draggedCard) moveTicket(draggedCard, zone.closest(".kanban-column").dataset.status);
    });
  });

  bulkApply?.addEventListener("click", async () => {
    const ticketIds = Array.from(board.querySelectorAll(".kanban-select:checked")).map((item) => Number(item.value));
    const status = board.querySelector("#bulk-status")?.value;
    const note = board.querySelector("#bulk-note")?.value.trim() || null;
    if (!ticketIds.length || !status) {
      showFeedback("Selecione ao menos uma demanda e o status de destino.", false);
      return;
    }
    bulkApply.disabled = true;
    try {
      const response = await fetch("/api/bulk/tickets/status", {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ticket_ids: ticketIds, status, note})});
      if (!response.ok) throw new Error(await responseError(response));
      showFeedback(`${ticketIds.length} demanda(s) atualizada(s).`, true);
      window.location.reload();
    } catch (error) {
      showFeedback(error.message || "Não foi possível aplicar a ação em lote.", false);
    } finally {
      bulkApply.disabled = false;
    }
  });

  updateColumnState();
});
