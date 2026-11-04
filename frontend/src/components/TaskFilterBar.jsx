import React from 'react';

export default function TaskFilterBar({
  projects,
  selectedProject,
  onSelectProject,
  meetings,
  selectedMeetingId,
  onSelectMeeting,
  statusFilter,
  onChangeStatusFilter,
  categoryFilter,
  onChangeCategoryFilter,
  assigneeFilter,
  onChangeAssigneeFilter,
  availableAssignees,
  searchQuery,
  onChangeSearchQuery,
  onResetFilters
}) {
  return (
    <div className="filter-bar">
      {/* Search Input */}
      <div className="search-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          type="text"
          placeholder="Search tasks, Jira keys, assignees..."
          value={searchQuery}
          onChange={(e) => onChangeSearchQuery(e.target.value)}
        />
      </div>

      {/* Project Selector */}
      <div className="select-group">
        <label>Project:</label>
        <select
          className="select-input"
          value={selectedProject}
          onChange={(e) => onSelectProject(e.target.value)}
        >
          {projects.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>

      {/* Meeting Selector */}
      <div className="select-group">
        <label>Meeting:</label>
        <select
          className="select-input"
          value={selectedMeetingId || 'latest'}
          onChange={(e) => onSelectMeeting(e.target.value)}
        >
          <option value="latest">Latest Meeting (Auto)</option>
          {meetings.map((m) => (
            <option key={m.meeting_id} value={m.meeting_id}>
              #{m.sequence_number || 1} {m.meeting_title || m.meeting_id}
            </option>
          ))}
        </select>
      </div>

      {/* Status Filter */}
      <div className="select-group">
        <label>Status:</label>
        <select
          className="select-input"
          value={statusFilter}
          onChange={(e) => onChangeStatusFilter(e.target.value)}
        >
          <option value="ALL">All Statuses</option>
          <option value="COMPLETED">Completed</option>
          <option value="IN_PROGRESS">In Progress</option>
          <option value="PENDING">Pending</option>
          <option value="BLOCKED">Blocked</option>
        </select>
      </div>

      {/* Category Filter */}
      <div className="select-group">
        <label>Category:</label>
        <select
          className="select-input"
          value={categoryFilter}
          onChange={(e) => onChangeCategoryFilter(e.target.value)}
        >
          <option value="ALL">All Categories</option>
          <option value="NEWLY_ASSIGNED">Newly Assigned</option>
          <option value="COMPLETED">Completed</option>
          <option value="IN_PROGRESS">In Progress</option>
          <option value="PENDING">Pending</option>
          <option value="CHANGED">Changed</option>
          <option value="NEEDS_REVIEW">Needs Review</option>
        </select>
      </div>

      {/* Assignee Filter */}
      {availableAssignees && availableAssignees.length > 0 && (
        <div className="select-group">
          <label>Assignee:</label>
          <select
            className="select-input"
            value={assigneeFilter}
            onChange={(e) => onChangeAssigneeFilter(e.target.value)}
          >
            <option value="ALL">All Assignees</option>
            {availableAssignees.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        </div>
      )}

      {/* Reset */}
      <div className="filter-actions">
        <button className="reset-filter-btn" onClick={onResetFilters}>
          Clear Filters
        </button>
      </div>
    </div>
  );
}

