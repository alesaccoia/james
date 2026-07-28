"use strict";

// Shared helper: fetch marketing events once, turn them into a
// chartjs-plugin-annotation config filtered by date range / scope text.
// Used by every page that renders a time-series Chart.js chart.
window.MarketingEvents = (function () {
  let cache = null;

  async function load() {
    if (cache) return cache;
    try {
      const r = await fetch('/eventi/data.json', { cache: 'no-store' });
      cache = (await r.json()).events || [];
    } catch {
      cache = [];
    }
    return cache;
  }

  // labels: array of x-axis date strings actually present on the chart —
  // only events matching one of them are drawn (avoids stray un-anchored lines).
  // scopeFilter: optional substring match against event.scope (case-insensitive).
  // An event with no scope at all is treated as general-purpose and always
  // shown, regardless of the filter — only a *non-matching* scope excludes it.
  function annotations(events, labels, scopeFilter) {
    const labelSet = new Set(labels);
    const out = {};
    let i = 0;
    events.forEach((e) => {
      if (!labelSet.has(e.date)) return;
      if (scopeFilter && e.scope && !e.scope.toLowerCase().includes(scopeFilter.toLowerCase())) return;
      out['evt' + (i++)] = {
        type: 'line',
        xMin: e.date, xMax: e.date,
        borderColor: '#94a3b8', borderWidth: 1, borderDash: [4, 3],
        label: {
          display: true, content: e.name, position: 'start', rotation: -90,
          font: { size: 10 }, backgroundColor: 'rgba(100,116,139,.85)', color: '#fff', padding: 4,
        },
      };
    });
    return out;
  }

  return { load, annotations };
})();
