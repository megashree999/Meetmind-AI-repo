import React from 'react';

export default function TaskComparisonTable({ tasks, onSelectTask }) {
  if (!tasks || tasks.length === 0) {
    return (
      <div className="table-container">
        <div className="empty-state">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
            <line x1="9" y1="15" x2="15" y2="15" />
          </svg>
          <p style={{ fontWeight: 600, fontSize: '1rem', color: 'var(--text-primary)' }}>
            No tasks match the selected criteria
          </p>
          <p style={{ fontSize: '0.85rem', marginTop: '4px' }}>
            Try adjusting search terms or resetting filters.
          </p>
        </div>
      </div>
    );
  }

  const getCategoryBadge = (cat) => {
    switch (cat) {
      case 'NEWLY_ASSIGNED':
        return <span className="badge badge-newly-assigned">Newly Assigned</span>;
      case 'COMPLETED':
        return <span className="badge badge-completed">Completed</span>;
      case 'IN_PROGRESS':
        return <span className="badge badge-in-progress">In Progress</span>;
      case 'PENDING':
        return <span className="badge badge-pending">Pending</span>;
      case 'CHANGED':
        return <span className="badge badge-changed">Changed</span>;
      case 'NEEDS_REVIEW':
        return <span className="badge badge-needs-review">Needs Review</span>;
      default:
        return <span className="badge">{cat || 'Active'}</span>;
    }
  };

  const getStatusBadge = (status) => {
    const s = (status || 'PENDING').toLowerCase();
    return (
      <span className={`status-badge ${s}`}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', backgroundColor: 'currentColor' }} />
        {status || 'PENDING'}
      </span>
    );
  };

  const getInitials = (name) => {
    if (!name) return '?';
    return name.split(' ').map((n) => n[0]).join('').substring(0, 2).toUpperCase();
  };

  return (
    <div className="table-container">
      <table className="tasks-table">
        <thead>
          <tr>
            <th style={{ width: '28%' }}>Task Name</th>
            <th style={{ width: '12%' }}>Jira Issue</th>
            <th style={{ width: '14%' }}>Assignee</th>
            <th style={{ width: '10%' }}>Prev Status</th>
            <th style={{ width: '10%' }}>Curr Status</th>
            <th style={{ width: '10%' }}>Deadline</th>
            <th style={{ width: '12%' }}>Category</th>
            <th style={{ width: '14%' }}>Reason / Evidence</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t, idx) => {
            const taskName = t.task_name || t.task || 'Untitled Task';
            const jiraKey = t.jira_issue_key || t.jira_key;
            const assignee = t.assignee || 'Unassigned';
            const prevStatus = t.previous_status || '—';
            const currStatus = t.current_status || t.status || 'PENDING';
            const deadline = t.deadline || '—';
            const category = t.classification || t.category || 'ACTIVE';
            const reason = t.reason || t.change_reason || t.completion_evidence || '—';

            return (
              <tr
                key={t.task_id || `${taskName}-${idx}`}
                className="task-row"
                onClick={() => onSelectTask(t)}
              >
                <td>
                  <span className="task-title">{taskName}</span>
                </td>
                <td>
                  {jiraKey ? (
                    t.jira_url ? (
                      <a
                        href={t.jira_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="jira-badge"
                        onClick={(e) => e.stopPropagation()}
                        title={`Open ${jiraKey} in Jira`}
                      >
                        {jiraKey}
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                          <polyline points="15 3 21 3 21 9" />
                          <line x1="10" y1="14" x2="21" y2="3" />
                        </svg>
                      </a>
                    ) : (
                      <span className="jira-badge">{jiraKey}</span>
                    )
                  ) : (
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>—</span>
                  )}
                </td>
                <td>
                  <div className="assignee-cell">
                    <div className="avatar">{getInitials(assignee)}</div>
                    <span>{assignee}</span>
                  </div>
                </td>
                <td>
                  {prevStatus !== '—' ? getStatusBadge(prevStatus) : <span style={{ color: 'var(--text-muted)' }}>—</span>}
                </td>
                <td>
                  {getStatusBadge(currStatus)}
                </td>
                <td>
                  <span style={{ fontSize: '0.82rem', fontFamily: 'var(--font-mono)' }}>
                    {deadline}
                  </span>
                </td>
                <td>
                  {getCategoryBadge(category)}
                </td>
                <td>
                  <span
                    style={{
                      fontSize: '0.82rem',
                      color: 'var(--text-secondary)',
                      display: '-webkit-box',
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden'
                    }}
                    title={reason}
                  >
                    {reason}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

