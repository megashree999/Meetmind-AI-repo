import React, { useState, useEffect, useMemo, useCallback } from 'react';
import OverviewCards from './components/OverviewCards';
import MeetingComparisonHeader from './components/MeetingComparisonHeader';
import TaskFilterBar from './components/TaskFilterBar';
import TaskComparisonTable from './components/TaskComparisonTable';
import TaskDetailsModal from './components/TaskDetailsModal';

export default function App() {
  const [projects, setProjects] = useState(['SCRUM']);
  const [selectedProject, setSelectedProject] = useState('SCRUM');
  const [meetings, setMeetings] = useState([]);
  const [selectedMeetingId, setSelectedMeetingId] = useState('latest');
  
  const [summaryCounts, setSummaryCounts] = useState(null);
  const [comparisonResult, setComparisonResult] = useState(null);
  const [comparisonStatus, setComparisonStatus] = useState('IDLE'); // 'IDLE', 'LOADING', 'SUCCESS', 'FAILED', 'EMPTY'
  const [comparisonError, setComparisonError] = useState(null);

  // Filters
  const [statusFilter, setStatusFilter] = useState('ALL');
  const [categoryFilter, setCategoryFilter] = useState('ALL');
  const [assigneeFilter, setAssigneeFilter] = useState('ALL');
  const [searchQuery, setSearchQuery] = useState('');

  // Auto-refresh interval (in seconds: 0 = Off, 5 = 5s, 10 = 10s, 30 = 30s)
  const [pollInterval, setPollInterval] = useState(8);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Selected task for modal
  const [activeTask, setActiveTask] = useState(null);

  // 1. Fetch Projects on initial load
  useEffect(() => {
    fetch('/api/projects')
      .then((res) => res.json())
      .then((data) => {
        if (data.status === 'SUCCESS' && data.projects?.length > 0) {
          setProjects(data.projects);
          if (!data.projects.includes(selectedProject)) {
            setSelectedProject(data.projects[0]);
          }
        }
      })
      .catch((err) => console.error('Error loading projects:', err));
  }, []);

  // 2. Fetch Meetings and Comparisons for selected project
  const loadData = useCallback(async (isManualRefresh = false) => {
    if (isManualRefresh) setIsRefreshing(true);

    try {
      // A. Fetch Meetings
      const meetingsRes = await fetch(`/api/meetings?project_key=${encodeURIComponent(selectedProject)}`);
      const meetingsData = await meetingsRes.json();
      const loadedMeetings = meetingsData.status === 'SUCCESS' ? (meetingsData.meetings || []) : [];
      setMeetings(loadedMeetings);

      // B. Fetch Summary
      const summaryRes = await fetch(`/api/summary?project_key=${encodeURIComponent(selectedProject)}`);
      const summaryData = await summaryRes.json();
      if (summaryData.status === 'SUCCESS') {
        setSummaryCounts(summaryData.summary);
      }

      // C. Fetch Comparison
      let compUrl = `/api/comparisons/latest?project_key=${encodeURIComponent(selectedProject)}`;
      if (selectedMeetingId && selectedMeetingId !== 'latest') {
        // Find previous meeting ID in loaded sequence
        const currIdx = loadedMeetings.findIndex((m) => m.meeting_id === selectedMeetingId);
        const prevId = currIdx > 0 ? loadedMeetings[currIdx - 1].meeting_id : '';
        compUrl = `/api/comparisons?project_key=${encodeURIComponent(selectedProject)}&current_meeting_id=${encodeURIComponent(selectedMeetingId)}${prevId ? `&previous_meeting_id=${encodeURIComponent(prevId)}` : ''}`;
      }

      const compRes = await fetch(compUrl);
      const compData = await compRes.json();

      if (compData.status === 'FAILED') {
        setComparisonStatus('FAILED');
        setComparisonError(compData.error || 'Sprint comparison failed.');
        setComparisonResult(null);
      } else if (compData.status === 'SUCCESS') {
        if (!compData.has_comparisons && !compData.comparison && loadedMeetings.length === 0) {
          setComparisonStatus('EMPTY');
          setComparisonResult(null);
        } else {
          setComparisonStatus('SUCCESS');
          setComparisonError(null);
          setComparisonResult(compData.comparison);
          if (compData.summary_counts) {
            setSummaryCounts((prev) => ({ ...prev, ...compData.summary_counts }));
          }
        }
      } else {
        setComparisonStatus('FAILED');
        setComparisonError(compData.message || 'Unexpected response');
      }
    } catch (err) {
      console.error('Error fetching dashboard data:', err);
      setComparisonStatus('FAILED');
      setComparisonError('Network connection error fetching sprint comparison.');
    } finally {
      if (isManualRefresh) {
        setTimeout(() => setIsRefreshing(false), 300);
      }
    }
  }, [selectedProject, selectedMeetingId]);

  // Trigger load when project or selected meeting changes
  useEffect(() => {
    loadData();
  }, [loadData]);

  // Configurable polling
  useEffect(() => {
    if (!pollInterval || pollInterval <= 0) return;
    const interval = setInterval(() => {
      loadData();
    }, pollInterval * 1000);
    return () => clearInterval(interval);
  }, [pollInterval, loadData]);

  // Extract all tasks across categories from comparisonResult
  const allTasks = useMemo(() => {
    if (!comparisonResult) return [];
    const list = [];
    const pushTasks = (arr, classification) => {
      (arr || []).forEach((t) => {
        list.push({ ...t, classification: t.classification || classification });
      });
    };

    pushTasks(comparisonResult.newly_assigned_tasks, 'NEWLY_ASSIGNED');
    pushTasks(comparisonResult.completed_tasks, 'COMPLETED');
    pushTasks(comparisonResult.in_progress_tasks, 'IN_PROGRESS');
    pushTasks(comparisonResult.pending_tasks, 'PENDING');
    pushTasks(comparisonResult.changed_tasks, 'CHANGED');
    pushTasks(comparisonResult.needs_review_tasks, 'NEEDS_REVIEW');

    return list;
  }, [comparisonResult]);

  // Unique list of assignees for filtering
  const availableAssignees = useMemo(() => {
    const s = new Set();
    allTasks.forEach((t) => {
      if (t.assignee && t.assignee !== 'Unassigned') s.add(t.assignee);
    });
    return Array.from(s).sort();
  }, [allTasks]);

  // Filter tasks based on search and dropdown selections
  const filteredTasks = useMemo(() => {
    return allTasks.filter((t) => {
      // 1. Status Filter
      if (statusFilter !== 'ALL') {
        const cur = (t.current_status || t.status || 'PENDING').toUpperCase();
        if (cur !== statusFilter) return false;
      }

      // 2. Category Filter
      if (categoryFilter !== 'ALL') {
        const cat = (t.classification || t.category || '').toUpperCase();
        if (cat !== categoryFilter) return false;
      }

      // 3. Assignee Filter
      if (assigneeFilter !== 'ALL') {
        if ((t.assignee || '') !== assigneeFilter) return false;
      }

      // 4. Search Query
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const name = (t.task_name || t.task || '').toLowerCase();
        const jira = (t.jira_issue_key || t.jira_key || '').toLowerCase();
        const assignee = (t.assignee || '').toLowerCase();
        const reason = (t.reason || t.change_reason || t.completion_evidence || '').toLowerCase();

        return name.includes(q) || jira.includes(q) || assignee.includes(q) || reason.includes(q);
      }

      return true;
    });
  }, [allTasks, statusFilter, categoryFilter, assigneeFilter, searchQuery]);

  // Resolve current and previous meeting objects for side-by-side header
  const { currentMeetingObj, previousMeetingObj } = useMemo(() => {
    if (!meetings || meetings.length === 0) {
      return { currentMeetingObj: null, previousMeetingObj: null };
    }

    if (selectedMeetingId && selectedMeetingId !== 'latest') {
      const idx = meetings.findIndex((m) => m.meeting_id === selectedMeetingId);
      const curr = idx >= 0 ? meetings[idx] : null;
      const prev = idx > 0 ? meetings[idx - 1] : null;
      return { currentMeetingObj: curr, previousMeetingObj: prev };
    }

    // Latest meeting
    const curr = meetings[meetings.length - 1];
    const prev = meetings.length > 1 ? meetings[meetings.length - 2] : null;
    return { currentMeetingObj: curr, previousMeetingObj: prev };
  }, [meetings, selectedMeetingId]);

  const handleResetFilters = () => {
    setStatusFilter('ALL');
    setCategoryFilter('ALL');
    setAssigneeFilter('ALL');
    setSearchQuery('');
  };

  return (
    <div className="dashboard-container">
      {/* Top Navigation */}
      <header className="top-nav">
        <div className="brand-section">
          <div className="brand-logo">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <span className="brand-title">MeetMind AI</span>
              <span className="brand-badge">Sprint Comparison</span>
            </div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Cross-Meeting Continuity & Jira Task Intelligence
            </div>
          </div>
        </div>

        <div className="nav-controls">
          {pollInterval > 0 && (
            <div className="live-indicator">
              <span className="pulse-dot" />
              <span>Live Sync ({pollInterval}s)</span>
            </div>
          )}

          <div className="select-group">
            <label>Auto-Sync:</label>
            <select
              className="select-input"
              value={pollInterval}
              onChange={(e) => setPollInterval(Number(e.target.value))}
            >
              <option value={0}>Manual Only</option>
              <option value={5}>Every 5s</option>
              <option value={8}>Every 8s</option>
              <option value={15}>Every 15s</option>
              <option value={30}>Every 30s</option>
            </select>
          </div>

          <button
            className={`refresh-btn ${isRefreshing ? 'spinning' : ''}`}
            onClick={() => loadData(true)}
            title="Refresh dashboard data"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M23 4v6h-6" />
              <path d="M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
            <span>Refresh</span>
          </button>
        </div>
      </header>

      {/* Honest Error State Banner */}
      {comparisonStatus === 'FAILED' && (
        <div className="alert-banner error">
          <div>
            <strong style={{ display: 'block', fontSize: '0.95rem' }}>
              Comparison Run Failure
            </strong>
            <p style={{ fontSize: '0.85rem', marginTop: '2px' }}>
              {comparisonError || 'The latest comparison encountered an error. Stale results are hidden to preserve audit integrity.'}
            </p>
          </div>
          <button className="refresh-btn" onClick={() => loadData(true)}>
            Retry Comparison
          </button>
        </div>
      )}

      {/* Overview Metrics Cards */}
      <OverviewCards counts={summaryCounts} />

      {/* Empty State Banner if zero meetings */}
      {comparisonStatus === 'EMPTY' && (
        <div className="table-container" style={{ marginBottom: 24 }}>
          <div className="empty-state">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
              <line x1="16" y1="2" x2="16" y2="6" />
              <line x1="8" y1="2" x2="8" y2="6" />
              <line x1="3" y1="10" x2="21" y2="10" />
            </svg>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, color: 'var(--text-primary)', marginTop: 8 }}>
              No Meetings Processed for Project {selectedProject}
            </h3>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', maxWidth: 460, margin: '6px auto 0' }}>
              Submit a meeting transcript through MeetMind AI to run extraction, task tracking, and sprint comparison automatically.
            </p>
          </div>
        </div>
      )}

      {/* Side-by-Side Meeting Comparison Header */}
      {comparisonStatus !== 'EMPTY' && (
        <MeetingComparisonHeader
          currentMeeting={currentMeetingObj}
          previousMeeting={previousMeetingObj}
        />
      )}

      {/* Filters and Search Bar */}
      {comparisonStatus !== 'EMPTY' && (
        <TaskFilterBar
          projects={projects}
          selectedProject={selectedProject}
          onSelectProject={(p) => {
            setSelectedProject(p);
            setSelectedMeetingId('latest');
          }}
          meetings={meetings}
          selectedMeetingId={selectedMeetingId}
          onSelectMeeting={setSelectedMeetingId}
          statusFilter={statusFilter}
          onChangeStatusFilter={setStatusFilter}
          categoryFilter={categoryFilter}
          onChangeCategoryFilter={setCategoryFilter}
          assigneeFilter={assigneeFilter}
          onChangeAssigneeFilter={setAssigneeFilter}
          availableAssignees={availableAssignees}
          searchQuery={searchQuery}
          onChangeSearchQuery={setSearchQuery}
          onResetFilters={handleResetFilters}
        />
      )}

      {/* Task Comparison Table */}
      {comparisonStatus !== 'EMPTY' && (
        <TaskComparisonTable
          tasks={filteredTasks}
          onSelectTask={setActiveTask}
        />
      )}

      {/* Task Details Modal / Drawer */}
      {activeTask && (
        <TaskDetailsModal
          task={activeTask}
          onClose={() => setActiveTask(null)}
        />
      )}
    </div>
  );
}

