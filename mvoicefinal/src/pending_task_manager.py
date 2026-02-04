# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# -------------------------------------------------------------------------
"""
Pending Task Manager - Manages background async tasks for agent queries.

This module provides task lifecycle management for non-blocking agent calls:
- Spawn tasks with timeout handling
- Track pending/completed/failed tasks
- Cancel tasks gracefully
- Emit status callbacks
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Awaitable, Any, Coroutine
from uuid import uuid4

from src.set_logging import logger


class TaskStatus(str, Enum):
    """Status of a pending task."""
    PENDING = "pending"       # Task created but not started
    RUNNING = "running"       # Task is executing
    COMPLETED = "completed"   # Task finished successfully
    FAILED = "failed"         # Task failed with error
    TIMEOUT = "timeout"       # Task exceeded timeout
    CANCELLED = "cancelled"   # Task was cancelled


@dataclass
class TaskResult:
    """Result of a completed task."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class PendingTask:
    """Represents a tracked async task."""
    task_id: str
    query: str
    task: asyncio.Task
    created_at: datetime
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[TaskResult] = None
    
    @property
    def age_seconds(self) -> float:
        """Time since task was created."""
        return (datetime.now() - self.created_at).total_seconds()


@dataclass
class TaskManagerConfig:
    """Configuration for the task manager."""
    max_concurrent_tasks: int = 3
    default_timeout: float = 30.0  # seconds
    cleanup_completed_after: float = 60.0  # seconds


# Callback type aliases
TaskStartCallback = Callable[[str, str], Awaitable[None]]  # (task_id, query)
TaskCompleteCallback = Callable[[str, str, TaskResult], Awaitable[None]]  # (task_id, query, result)
TaskErrorCallback = Callable[[str, str, str], Awaitable[None]]  # (task_id, query, error)


class PendingTaskManager:
    """
    Manages background async tasks with lifecycle tracking.
    
    This manager handles spawning, tracking, and cleanup of async tasks
    that run agent queries in the background. It supports:
    - Concurrent task limits
    - Timeout handling
    - Status callbacks for UI updates
    - Graceful cancellation
    
    Usage:
        manager = PendingTaskManager()
        
        # Register callbacks for UI updates
        manager.on_task_start(async_start_handler)
        manager.on_task_complete(async_complete_handler)
        
        # Spawn a task
        task_id = await manager.spawn(
            query="Find customer 12345",
            coroutine=agent.ainvoke({"messages": [...]}),
        )
        
        # Check status
        status = manager.get_task_status(task_id)
        
        # Cleanup when done
        await manager.shutdown()
    """
    
    def __init__(self, config: Optional[TaskManagerConfig] = None):
        self.config = config or TaskManagerConfig()
        self._tasks: dict[str, PendingTask] = {}
        self._task_counter = 0
        
        # Callbacks
        self._on_start: Optional[TaskStartCallback] = None
        self._on_complete: Optional[TaskCompleteCallback] = None
        self._on_error: Optional[TaskErrorCallback] = None
        
        # Cleanup task
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
    
    @property
    def pending_count(self) -> int:
        """Number of currently pending/running tasks."""
        return sum(
            1 for t in self._tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        )
    
    @property
    def can_accept_task(self) -> bool:
        """Check if manager can accept another task."""
        return self.pending_count < self.config.max_concurrent_tasks
    
    def on_task_start(self, callback: TaskStartCallback) -> None:
        """Register callback for task start events."""
        self._on_start = callback
    
    def on_task_complete(self, callback: TaskCompleteCallback) -> None:
        """Register callback for task completion events."""
        self._on_complete = callback
    
    def on_task_error(self, callback: TaskErrorCallback) -> None:
        """Register callback for task error events."""
        self._on_error = callback
    
    async def start(self) -> None:
        """Start the task manager (enables cleanup)."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("PendingTaskManager started")
    
    async def shutdown(self) -> None:
        """Stop the task manager and cancel all tasks."""
        self._running = False
        
        # Cancel cleanup task
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all pending tasks
        for task_id, pending in list(self._tasks.items()):
            if pending.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                await self.cancel_task(task_id)
        
        self._tasks.clear()
        logger.info("PendingTaskManager shutdown complete")
    
    async def spawn(
        self,
        query: str,
        coroutine: Coroutine[Any, Any, Any],
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """
        Spawn a new background task.
        
        Args:
            query: Description of the query (for logging/callbacks)
            coroutine: The async coroutine to run
            timeout: Optional timeout override
            
        Returns:
            Task ID if spawned, None if at capacity
        """
        if not self.can_accept_task:
            logger.warning(
                f"Cannot spawn task: at capacity "
                f"({self.pending_count}/{self.config.max_concurrent_tasks})"
            )
            return None
        
        # Generate unique task ID
        self._task_counter += 1
        task_id = f"task_{self._task_counter}_{uuid4().hex[:8]}"
        
        # Create the wrapped task with timeout
        effective_timeout = timeout or self.config.default_timeout
        wrapped = self._run_with_tracking(task_id, query, coroutine, effective_timeout)
        task = asyncio.create_task(wrapped)
        
        # Track the task
        pending = PendingTask(
            task_id=task_id,
            query=query,
            task=task,
            created_at=datetime.now(),
            status=TaskStatus.RUNNING,
        )
        self._tasks[task_id] = pending
        
        logger.info(f"Spawned task {task_id}: {query[:50]}...")
        
        # Emit start callback
        if self._on_start:
            try:
                await self._on_start(task_id, query)
            except Exception as e:
                logger.error(f"Error in task start callback: {e}")
        
        return task_id
    
    async def _run_with_tracking(
        self,
        task_id: str,
        query: str,
        coroutine: Coroutine[Any, Any, Any],
        timeout: float,
    ) -> None:
        """Run a coroutine with timeout and status tracking."""
        start_time = datetime.now()
        pending = self._tasks.get(task_id)
        
        if not pending:
            return
        
        try:
            # Run with timeout
            result_data = await asyncio.wait_for(coroutine, timeout=timeout)
            
            # Success
            duration = (datetime.now() - start_time).total_seconds() * 1000
            result = TaskResult(
                success=True,
                data=result_data,
                duration_ms=duration,
            )
            
            pending.status = TaskStatus.COMPLETED
            pending.result = result
            
            logger.info(f"Task {task_id} completed in {duration:.0f}ms")
            
            # Emit completion callback
            if self._on_complete:
                try:
                    await self._on_complete(task_id, query, result)
                except Exception as e:
                    logger.error(f"Error in task complete callback: {e}")
                    
        except asyncio.TimeoutError:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            pending.status = TaskStatus.TIMEOUT
            pending.result = TaskResult(
                success=False,
                error=f"Task timed out after {timeout}s",
                duration_ms=duration,
            )
            
            logger.warning(f"Task {task_id} timed out after {timeout}s")
            
            if self._on_error:
                try:
                    await self._on_error(task_id, query, f"Timeout after {timeout}s")
                except Exception as e:
                    logger.error(f"Error in task error callback: {e}")
                    
        except asyncio.CancelledError:
            pending.status = TaskStatus.CANCELLED
            logger.info(f"Task {task_id} was cancelled")
            raise
            
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = str(e)
            
            pending.status = TaskStatus.FAILED
            pending.result = TaskResult(
                success=False,
                error=error_msg,
                duration_ms=duration,
            )
            
            logger.error(f"Task {task_id} failed: {error_msg}")
            
            if self._on_error:
                try:
                    await self._on_error(task_id, query, error_msg)
                except Exception as cb_error:
                    logger.error(f"Error in task error callback: {cb_error}")
    
    def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        """Get the status of a task."""
        pending = self._tasks.get(task_id)
        return pending.status if pending else None
    
    def get_task_result(self, task_id: str) -> Optional[TaskResult]:
        """Get the result of a completed task."""
        pending = self._tasks.get(task_id)
        return pending.result if pending else None
    
    def get_pending_queries(self) -> list[str]:
        """Get list of queries for all pending/running tasks."""
        return [
            t.query for t in self._tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        ]
    
    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a specific task."""
        pending = self._tasks.get(task_id)
        if not pending:
            return False
        
        if pending.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
            return False
        
        if not pending.task.done():
            pending.task.cancel()
            try:
                await pending.task
            except asyncio.CancelledError:
                pass
        
        pending.status = TaskStatus.CANCELLED
        logger.info(f"Cancelled task {task_id}")
        return True
    
    async def cancel_all(self) -> int:
        """Cancel all pending/running tasks. Returns count cancelled."""
        count = 0
        for task_id in list(self._tasks.keys()):
            if await self.cancel_task(task_id):
                count += 1
        return count
    
    async def _cleanup_loop(self) -> None:
        """Background loop to clean up old completed tasks."""
        try:
            while self._running:
                await asyncio.sleep(30)  # Check every 30 seconds
                await self._cleanup_old_tasks()
        except asyncio.CancelledError:
            pass
    
    async def _cleanup_old_tasks(self) -> None:
        """Remove old completed/failed tasks."""
        now = datetime.now()
        to_remove = []
        
        for task_id, pending in self._tasks.items():
            if pending.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, 
                                  TaskStatus.TIMEOUT, TaskStatus.CANCELLED):
                age = (now - pending.created_at).total_seconds()
                if age > self.config.cleanup_completed_after:
                    to_remove.append(task_id)
        
        for task_id in to_remove:
            del self._tasks[task_id]
            logger.debug(f"Cleaned up old task {task_id}")
