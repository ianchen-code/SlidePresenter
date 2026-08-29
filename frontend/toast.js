// Shared toast notifications, styled like shadcn/sonner: a white card with a
// small colored icon badge rather than a solid colored background. Stack
// sits fixed bottom-right, each toast is width-capped (not full width).
//
// showToast(message, { type: 'info' | 'success' | 'error' | 'share', duration })
// The 'share' type gets an accent ring so it stands out from the rest --
// for actions the user should really notice, e.g. turning link sharing on.

function getToastContainer() {
  let el = document.getElementById('toastContainer');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toastContainer';
    el.className = 'fixed bottom-16 right-6 z-[200] flex flex-col items-end gap-2 pointer-events-none';
    // A screen reader announces each toast's text as it's added, without
    // stealing focus from whatever the user was doing.
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.setAttribute('aria-atomic', 'true');
    document.body.appendChild(el);
  }
  return el;
}

const TOAST_ICON_WRAP = {
  info: 'bg-stone-100 text-stone-500',
  success: 'bg-emerald-100 text-emerald-600',
  error: 'bg-red-100 text-red-600',
  share: 'bg-accent/15 text-accent',
};

// Icon glyphs only (Lucide-style, stroke-based) -- the colored circular
// badge that wraps these already supplies the "circle", so these are just
// the inner mark to avoid drawing two overlapping circles.
const TOAST_ICON_PATH = {
  info: '<path d="M12 16v-4" /><path d="M12 8h.01" />',
  success: '<path d="M20 6 9 17l-5-5" />',
  error: '<path d="M12 8v4" /><path d="M12 16h.01" />',
  share: '<path d="M9 17H7a5 5 0 0 1 0-10h2" /><path d="M15 7h2a5 5 0 1 1 0 10h-2" /><path d="M8 12h8" />',
};

function showToast(message, { type = 'info', duration = 3500 } = {}) {
  const container = getToastContainer();
  const toast = document.createElement('div');
  const iconWrap = TOAST_ICON_WRAP[type] || TOAST_ICON_WRAP.info;
  const iconPath = TOAST_ICON_PATH[type] || TOAST_ICON_PATH.info;
  const ring = type === 'share' ? ' ring-1 ring-accent/30' : '';

  // w-max lets short toasts hug their content; min-w-0 on the message span
  // is what actually lets a flex child wrap instead of forcing the row
  // wider than max-w-sm, and overflow-wrap:anywhere covers long unbroken
  // tokens (URLs, ids) that a normal word-wrap wouldn't break on.
  toast.className = `pointer-events-auto bg-white border border-line${ring} rounded-lg shadow-lg px-3.5 py-3 flex items-start gap-2.5 max-w-sm w-max transition-all duration-200 opacity-0 translate-y-2 scale-95`;
  toast.innerHTML = `
    <span class="flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center ${iconWrap}">
      <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${iconPath}</svg>
    </span>
    <span class="toast-message min-w-0 flex-1 text-sm font-medium text-ink leading-snug pt-px break-words [overflow-wrap:anywhere]"></span>
    <div class="flex items-center gap-0.5 flex-shrink-0 -mr-1 -mt-0.5">
      <button type="button" class="toast-copy text-stone-400 hover:text-stone-600 p-0.5" aria-label="Copy message">
        <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect width="14" height="14" x="8" y="8" rx="2" ry="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />
        </svg>
      </button>
      <button type="button" class="toast-close text-stone-400 hover:text-stone-600 p-0.5" aria-label="Dismiss">
        <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M18 6 6 18" /><path d="m6 6 12 12" />
        </svg>
      </button>
    </div>
  `;
  toast.querySelector('.toast-message').textContent = message;

  container.appendChild(toast);
  toast.offsetHeight; // force reflow so the entrance transition plays
  toast.classList.remove('opacity-0', 'translate-y-2', 'scale-95');

  const dismiss = () => {
    toast.classList.add('opacity-0', 'scale-95');
    setTimeout(() => toast.remove(), 200);
  };

  const copyBtn = toast.querySelector('.toast-copy');
  copyBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const original = copyBtn.innerHTML;
    try {
      await navigator.clipboard.writeText(message);
      copyBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg>';
    } catch (err) {
      // clipboard permission denied or unavailable -- nothing else to do
    }
    setTimeout(() => { copyBtn.innerHTML = original; }, 1200);
  });

  toast.querySelector('.toast-close').addEventListener('click', (e) => {
    e.stopPropagation();
    dismiss();
  });
  toast.addEventListener('click', dismiss);
  setTimeout(dismiss, duration);
}
