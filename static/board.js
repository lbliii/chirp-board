(() => {
  const root = document.querySelector("[data-board-root]");
  if (!root) return;

  const cards = () =>
    Array.from(root.querySelectorAll("[data-issue-url]")).filter(
      (node) => node.matches("li, tr"),
    );

  let index = -1;

  const focusAt = (next) => {
    const items = cards();
    if (!items.length) return;
    index = (next + items.length) % items.length;
    items[index].focus();
  };

  const openFocused = () => {
    const items = cards();
    if (index < 0 || index >= items.length) return;
    const url = items[index].getAttribute("data-issue-url");
    if (url) window.location.href = url;
  };

  const openMove = () => {
    const items = cards();
    if (index < 0 || index >= items.length) return;
    const details = items[index].querySelector("details.board-move");
    if (details) {
      details.open = true;
      const select = details.querySelector("select");
      if (select) select.focus();
    }
  };

  document.addEventListener("keydown", (event) => {
    const target = event.target;
    if (
      target instanceof HTMLElement &&
      (target.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
    ) {
      return;
    }
    if (event.key === "/") {
      event.preventDefault();
      const search = document.querySelector("[data-board-search]");
      if (search instanceof HTMLInputElement) search.focus();
      return;
    }
    if (event.key === "c") {
      const create = document.querySelector('a.board-button[href$="/issues/new"]');
      if (create instanceof HTMLAnchorElement) {
        event.preventDefault();
        window.location.href = create.href;
      }
      return;
    }
    if (event.key === "j") {
      event.preventDefault();
      focusAt(index + 1);
      return;
    }
    if (event.key === "k") {
      event.preventDefault();
      focusAt(index <= 0 ? cards().length - 1 : index - 1);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      openFocused();
      return;
    }
    if (event.key === "m") {
      event.preventDefault();
      openMove();
    }
  });
})();
