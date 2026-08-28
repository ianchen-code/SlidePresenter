// Shared open/close transition helpers for modal dialogs and dropdown menus.
// Elements are expected to start with the Tailwind classes:
//   dialog overlay:  hidden fixed ... transition-opacity duration-200 opacity-0
//   dialog panel (overlay's first child): transition-all duration-200 scale-95 opacity-0
//   menu:            hidden ... transition-all duration-150 opacity-0 scale-95

function openDialog(overlay) {
  const panel = overlay.firstElementChild;
  overlay.classList.remove('hidden');
  overlay.classList.add('flex');
  overlay.offsetHeight; // force reflow so the fade-in actually transitions
  overlay.classList.remove('opacity-0');
  panel?.classList.remove('opacity-0', 'scale-95');
}

function closeDialog(overlay) {
  const panel = overlay.firstElementChild;
  overlay.classList.add('opacity-0');
  panel?.classList.add('opacity-0', 'scale-95');
  setTimeout(() => {
    overlay.classList.add('hidden');
    overlay.classList.remove('flex');
  }, 200);
}

function openMenu(menu) {
  if (!menu) return;
  menu.classList.remove('hidden');
  menu.offsetHeight; // force reflow
  menu.classList.remove('opacity-0', 'scale-95');
}

function closeMenu(menu) {
  if (!menu || menu.classList.contains('hidden')) return;
  menu.classList.add('opacity-0', 'scale-95');
  setTimeout(() => menu.classList.add('hidden'), 150);
}
