import React from 'react';

export default function OverviewCards({ counts }) {
  const c = counts || {};
  const metrics = [
    {
      label: 'Active Tasks',
      value: c.total_active_tasks ?? c.active ?? (Number(c.pending || c.pending_tasks || 0) + Number(c.in_progress || c.in_progress_tasks || 0) + Number(c.newly_assigned || c.newly_assigned_tasks || 0)),
      color: '#2563eb',
      bg: '#eff6ff',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <rect x="2" y="7" width="20" height="14" rx="2" ry="2" />
          <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
        </svg>
      )
    },
    {
      label: 'Newly Assigned',
      value: c.newly_assigned ?? c.newly_assigned_tasks ?? 0,
      color: '#0284c7',
      bg: '#f0f9ff',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="16" />
          <line x1="8" y1="12" x2="16" y2="12" />
        </svg>
      )
    },
    {
      label: 'Completed',
      value: c.completed ?? c.completed_tasks ?? 0,
      color: '#16a34a',
      bg: '#f0fdf4',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
          <polyline points="22 4 12 14.01 9 11.01" />
        </svg>
      )
    },
    {
      label: 'In Progress',
      value: c.in_progress ?? c.in_progress_tasks ?? 0,
      color: '#7c3aed',
      bg: '#f5f3ff',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <circle cx="12" cy="12" r="10" />
          <polyline points="12 6 12 12 16 14" />
        </svg>
      )
    },
    {
      label: 'Pending',
      value: c.pending ?? c.pending_tasks ?? 0,
      color: '#d97706',
      bg: '#fffbeb',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="8" x2="12" y2="12" />
          <line x1="12" y1="16" x2="12.01" y2="16" />
        </svg>
      )
    },
    {
      label: 'Changed',
      value: c.changed ?? c.changed_tasks ?? 0,
      color: '#ea580c',
      bg: '#fff7ed',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <polyline points="23 4 23 10 17 10" />
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
      )
    },
    {
      label: 'Needs Review',
      value: c.needs_review ?? c.needs_review_tasks ?? 0,
      color: '#dc2626',
      bg: '#fef2f2',
      icon: (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
      )
    }
  ];

  return (
    <div className="metrics-grid">
      {metrics.map((m, idx) => (
        <div key={idx} className="metric-card">
          <div className="metric-header">
            <span className="metric-label">{m.label}</span>
            <div className="metric-icon" style={{ backgroundColor: m.bg, color: m.color }}>
              {m.icon}
            </div>
          </div>
          <div className="metric-value">{m.value}</div>
        </div>
      ))}
    </div>
  );
}

