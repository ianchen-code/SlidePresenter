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

const TOAST_ICON_PATH = {
  info: '<path stroke-linecap="round" stroke-linejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />',
  success: '<path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />',
  error: '<path stroke-linecap="round" stroke-linejoin="round" d="M9.75 9.75l4.5 4.5m0-4.5l-4.5 4.5M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />',
  share: '<path stroke-linecap="round" stroke-linejoin="round" d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244" />',
};

function showToast(message, { type = 'info', duration = 3500 } = {}) {
  const container = getToastContainer();
  const toast = document.createElement('div');
  const iconWrap = TOAST_ICON_WRAP[type] || TOAST_ICON_WRAP.info;
  const iconPath = TOAST_ICON_PATH[type] || TOAST_ICON_PATH.info;
  const ring = type === 'share' ? ' ring-1 ring-accent/30' : '';

  toast.className = `pointer-events-auto bg-white border border-line${ring} rounded-lg shadow-lg px-3.5 py-3 flex items-start gap-2.5 max-w-sm w-max transition-all duration-200 opacity-0 translate-y-2 scale-95`;
  toast.innerHTML = `
    <span class="flex-shrink-0 w-5 h-5 rounded-full flex items-center justify-center ${iconWrap}">
      <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">${iconPath}</svg>
    </span>
    <span class="toast-message text-sm font-medium text-ink leading-snug pt-px"></span>
    <button type="button" class="toast-close flex-shrink-0 text-stone-400 hover:text-stone-600 -mr-1 -mt-0.5 p-0.5" aria-label="Dismiss">
      <svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  `;
  toast.querySelector('.toast-message').textContent = message;

  container.appendChild(toast);
  toast.offsetHeight; // force reflow so the entrance transition plays
  toast.classList.remove('opacity-0', 'translate-y-2', 'scale-95');

  const dismiss = () => {
    toast.classList.add('opacity-0', 'scale-95');
    setTimeout(() => toast.remove(), 200);
  };
  toast.querySelector('.toast-close').addEventListener('click', (e) => {
    e.stopPropagation();
    dismiss();
  });
  toast.addEventListener('click', dismiss);
  setTimeout(dismiss, duration);
}
