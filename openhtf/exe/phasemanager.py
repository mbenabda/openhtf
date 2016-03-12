# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""PhaseExecutor module for handling the phases of a test.

Each phase is an instance of phase_data.PhaseInfo and therefore has relevant
options. Each option is taken into account when executing a phase, such as
checking options.run_if as soon as possible and timing out at the appropriate
time.

A phase must return an openhtf.PhaseResult, one of CONTINUE, REPEAT, or FAIL.
A phase may also return None, or have no return statement, which is the same as
returning openhtf.PhaseResult.CONTINUE.  These results are then acted upon
accordingly and a new test run status is returned.

Phases are always run in order and not allowed to loop back, though a phase may
choose to repeat itself by returning REPEAT. Returning FAIL will cause a test to
fail early, allowing a test to detect a bad state and not waste any further
time. A phase should not return TIMEOUT or ABORT, those are handled by the
framework.
"""

import collections
import inspect
import logging

import gflags
import mutablerecords

import openhtf
from openhtf.exe import phase_data
from openhtf.io import test_record
from openhtf.util import threads


FLAGS = gflags.FLAGS
gflags.DEFINE_integer('phase_default_timeout_ms', 3 * 60 * 1000,
                      'Test phase timeout in ms', lower_bound=0)

_LOG = logging.getLogger(__name__)


class InvalidPhaseResultError(Exception):
  """Raised when a PhaseOutcome is created with an invalid phase result."""


class PhaseOutcome(collections.namedtuple(
    'PhaseOutcome', 'phase_result')):
  """Provide some utility and sanity around phase return values.

  This should not be confused with openhtf.PhaseResult.  PhaseResult is an
  enumeration to provide user-facing valid phase return values.  This tuple
  is used internally to track other possible outcomes (timeout, exception),
  and to perform some sanity checking (weird return values from phases).

  If phase_result is None, that indicates the phase timed out (this makes
  sense if you think about it, it timed out, so there was no result).  If
  phase_result is an instance of Exception, then that is the Exception that
  was raised by the phase.  The raised_exception attribute can be used as
  a convenience to test for that condition, and the is_timeout attribute can
  similarly be used to check for the timeout case.

  The only accepted values for phase_result are None (timeout), an instance
  of Exception (phase raised), or an instance of openhtf.PhaseResult.  Any
  other value will raise an InvalidPhaseResultError.
  """
  def __init__(self, phase_result):
    if not (phase_result is None or
            isinstance(phase_result, Exception) or
            (isinstance(phase_result, openhtf.PhaseResult) and
             phase_result.valid_phase_return)):
      raise InvalidPhaseResultError('Invalid phase result', phase_result)
    super(PhaseOutcome, self).__init__(phase_result)

  @property
  def is_timeout(self):
    """True if this PhaseOutcome indicates a phase timeout."""
    return self.phase_result is None

  @property
  def raised_exception(self):
    """True if the phase in question raised an exception."""
    return isinstance(self.phase_result, Exception)


class PhaseExecutorThread(threads.KillableThread):
  """Handles the execution and result of a single test phase.

  The thread's result will be stored in phase_thread.result after it's finished,
  DIDNT_FINISH until then. It will be an instance of PhaseOutcome.
  """

  def __init__(self, phase, phase_data):
    self._phase = phase
    self._phase_data = phase_data
    self._phase_outcome = None
    super(PhaseExecutorThread, self).__init__(
        name='PhaseThread: %s' % self.name)

  def _ThreadProc(self):
    """Execute the encompassed phase and save the result."""
    # Call the phase, save the return value, or default it to CONTINUE.
    phase_return = self._phase(self._phase_data)
    if phase_return is None:
      phase_return = openhtf.PhaseResult.CONTINUE

    # Pop any things out of the exit stack and close them.
    self._phase_data.context.pop_all().close()

    # If phase_return is invalid, this will raise, and _phase_outcome will get
    # set to the InvalidPhaseResultError in _ThreadException instead.
    self._phase_outcome = PhaseOutcome(phase_return)

  def _ThreadException(self, exc):
    self._phase_outcome = PhaseOutcome(exc)
    self._phase_data.logger.exception('Phase %s raised an exception', self.name)

  def JoinOrDie(self):
    """Wait for thread to finish, return a PhaseOutcome with its response."""
    if self._phase.options.timeout_s is not None:
      self.join(self._phase.options.timeout_s)
    else:
      self.join(FLAGS.phase_default_timeout_ms / 1000.0)

    # We got a return value or an exception and handled it.
    if isinstance(self._phase_outcome, PhaseOutcome):
      return self._phase_outcome

    # Check for timeout, indicated by None for PhaseOutcome.phase_result.
    if self.is_alive():
      self.Kill()
      return PhaseOutcome(None)

    # Phase was killed.
    return PhaseOutcome(threads.ThreadTerminationError())

  @property
  def name(self):
    return self._phase.name

  def __str__(self):
    return '<%s: (%s)>' % (type(self).__name__, self.name)
  __repr__ = __str__


class PhaseExecutor(mutablerecords.Record(
    'PhaseExecutor', ['_config', 'test_state'], {'_current_phase': None})):
  """Encompasses the execution of the phases of a test."""
  # TODO(madsci): continue removing mutablerecords here.
  def __init__(self, test_state):
    self.test_state = test_state
    self.current_phase = None

  def ExecutePhases(self):
    """Executes each phase or skips them, yielding PhaseOutcome instances.

    Yields:
      PhaseOutcome instance that wraps the phase return value (or exception).
    """
    while self.test_state.pending_phases:
      result = self._ExecuteOnePhase(self.test_state.pending_phases[0])
      if not result:
        continue
      yield result

  def _ExecuteOnePhase(self, phase):
    """Executes the given phase, returning a PhaseOutcome."""
    phase_data = self.test_state.phase_data

    # Check this as early as possible.
    if phase.options.run_if and not phase.options.run_if(phase_data):
      _LOG.info('Phase %s skipped due to run_if returning falsey.', phase.name)
      self.test_state.pending_phases.pop(0)
      return

    _LOG.info('Executing phase %s with plugs %s', phase.name, phase_data.plugs)

    self._test_state.running_phase = test_record.PhaseRecord(
        phase.name, phase.code_info)

    with phase_data.RecordPhaseTiming(phase, self.test_state) as outcome_wrapper:
      phase_thread = PhaseExecutorThread(phase, phase_data)
      phase_thread.start()
      self._current_phase = phase_thread
      outcome_wrapper.SetOutcome(phase_thread.JoinOrDie())

    if outcome_wrapper.outcome.phase_result == openhtf.PhaseResult.CONTINUE:
      self.test_state.pending_phases.pop(0)

    _LOG.debug('Phase finished with outcome %s', outcome_wrapper.outcome)
    return outcome_wrapper.outcome

  def Stop(self):
    """Stops the current phase."""
    if self._current_phase:
      self._current_phase.Kill()
