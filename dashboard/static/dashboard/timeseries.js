"use strict";

// Shared helpers for period/granularity controls and continuous timelines -
// used by every page with a day/week/month toggle, so sparse series (e.g. a
// metric that only has data on days something happened) don't make a chart
// skip straight from one data point to the next across weeks of nothing.
window.TimeSeries = (function () {
  function daysAgo(n) {
    const d = new Date();
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() - n);
    return d.toISOString().slice(0, 10);
  }

  function todayStr() {
    return new Date().toISOString().slice(0, 10);
  }

  // groupBy: "day" | "week" | "month". Week buckets land on their Monday.
  function bucketOf(dateStr, groupBy) {
    if (groupBy === "month") return dateStr.slice(0, 7);
    if (groupBy !== "week") return dateStr;
    const d = new Date(dateStr + "T00:00:00Z");
    const day = d.getUTCDay();
    d.setUTCDate(d.getUTCDate() + ((day === 0 ? -6 : 1) - day)); // back to Monday
    return d.toISOString().slice(0, 10);
  }

  // Every calendar day from start to end (inclusive), YYYY-MM-DD.
  function allDaysInRange(startStr, endStr) {
    const days = [];
    const d = new Date(startStr + "T00:00:00Z");
    const end = new Date(endStr + "T00:00:00Z");
    while (d <= end) {
      days.push(d.toISOString().slice(0, 10));
      d.setUTCDate(d.getUTCDate() + 1);
    }
    return days;
  }

  // Continuous list of buckets (day/week/month) covering [startStr, today],
  // deduped and sorted - the x-axis to render against regardless of which
  // dates actually have data.
  function continuousBuckets(startStr, groupBy, endStr) {
    return [...new Set(allDaysInRange(startStr, endStr || todayStr()).map(d => bucketOf(d, groupBy)))].sort();
  }

  function renderGroupTabs(el, groupBy, onChange) {
    el.querySelectorAll("[data-g]").forEach(b => {
      b.classList.toggle("btn-active", b.dataset.g === groupBy);
      b.onclick = () => onChange(b.dataset.g);
    });
  }

  return { daysAgo, todayStr, bucketOf, allDaysInRange, continuousBuckets, renderGroupTabs };
})();
