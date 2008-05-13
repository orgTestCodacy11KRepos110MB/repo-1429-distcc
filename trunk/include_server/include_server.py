#!/usr/bin/python2.4

# Copyright 2007 Google Inc.
# 
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.

"""Conservative approximation of include dependencies for C/C++."""

__author__ = "Nils Klarlund"

# TODO (klarlund) Implement abort mechanism: regularly check whether
# ppid is 0; if so, then abort.

# Python imports
import os
import re
import sys
import glob
import signal
import getopt
import tempfile
import traceback
import SocketServer

# Include server imports
import basics
import statistics
import include_analyzer_memoizing_node
import distcc_pump_c_extensions

# The default size passed to listen by a streaming socket server of
# SocketServer is only 5. Make it 128 (which appears to be the hard
# built-in limit for Linux). This enables requests to the include
# server to be buffered better.
REQUEST_QUEUE_SIZE = 128

Debug = basics.Debug
DEBUG_TRACE = basics.DEBUG_TRACE
DEBUG_WARNING = basics.DEBUG_WARNING
# Exceptions.
SignalSIGTERM = basics.SignalSIGTERM
NotCoveredError = basics.NotCoveredError
NotCoveredTimeOutError = basics.NotCoveredTimeOutError


# USAGE

def Usage():
  print """Usage:

include_server --port INCLUDE_SERVER_PORT [options]

where INCLUDE_SERVER_PORT is a socket name. Fork the include server
for incremental include analysis. The include server answers queries
from the distcc client about which files to include in a C/C++
compilation. This command itself terminates as soon as the include
server has been spawned.

Options:
 --pid_file FILEPATH         The pid of the include server is written to file
                             FILEPATH.
 -dPAT, --debug_pattern=PAT  Bit vector for turning on warnings and debugging
                               1 = warnings
                               2 = trace some functions
                             other powers of two: see basics.py.
 -e, --email                 Send email to discc-pump developers when include
                             server gets in trouble.
 --no-email                  Do not send email.
 --email_bound NUMBER        Maximal number of emails to send (in addition to
                             a final email). Default: 3.
 --realpath_warning_re=RE    Write a warning to stderr whenever a filename is
                             resolved to a realpath that is matched by RE,
                             which is a regular expression in Python syntax.
                             (Warnings must be enabled with at least -d1.)
 --stat_reset_triggers=LIST  Flush stat caches when the timestamp of any
                             filepath in LIST changes or the filepath comes in
                             or out of existence.  LIST is a colon separated
                             string of filepaths, possibly containing simple
                             globs (as allowed by Python's glob module). Print
                             a warning whenever such a change happens (if
                             warnings are enabled). This option allows limited
                             exceptions to distcc_pump's normal assumption that
                             source files are not modified during the build.
 -x, --exact_analysis        Use CPP instead, do not omit system headers files.
 -v, --verify                Verify that files in CPP closure are contained in
                             closure calculated by include processor.
 -s, --statistics            Print information to stdout about include analysis.
 -t, --time                  Print elapsed, user, and system time to stderr.
 -w, --write_include_closure Write a .d_approx file which lists all the
                             included files calculated by the include server;
                             with -x, additionally write the included files
                             as calculated by CPP to a .d_exact file.
"""

# TODO(klarlund)
#   --simple_algorithm         not currently implemented


# UTILITIES

def _PrintStackTrace(fd):
  """Print stacktrace to file object."""
  print >> fd, '------- Include server stack trace -----------'
  # Limit is 1000 entries.
  traceback.print_exc(1000, fd)
  print >> fd, '----------------------------------------------'


class _EmailSender(object):
  """For sending emails. We limit their number to avoid email storms."""

  def __init__(self):
    self.number_sent = 0

  def TryToSend(self, fd, force=False, never=False):
    """Send the contents of file to predefined blame address.
    Arguments:
      fd: open file descriptor, will remain open
      force: send even if bound has been reached
    """
    if not basics.opt_send_email: return
    if self.number_sent >= basics.opt_email_bound and not force: return
    if never: return
    self.number_sent += 1
    # For efficiency, we postpone reading needed libraries for emailing until
    # now.
    import smtplib
    import getpass
    import socket
    try:
      user_addr = "%s@%s" %  (getpass.getuser(), socket.gethostname())
      fd.seek(0)
      msg = "Subject: %s\nTo: %s\nFrom: %s\n\n%s\n%s" % (
        basics.EMAIL_SUBJECT,
        basics.DCC_EMAILLOG_WHOM_TO_BLAME,
        user_addr,
        "Automated email number %d in include server session.\n" % 
        self.number_sent,
        fd.read())
      s = smtplib.SMTP()
      s.connect()
      s.sendmail(user_addr, [basics.DCC_EMAILLOG_WHOM_TO_BLAME], msg)
      Debug(DEBUG_WARNING, "Include server sent email to %s",
            basics.DCC_EMAILLOG_WHOM_TO_BLAME)
      s.close()
    except:
      Debug(DEBUG_WARNING, basics.CANT_SEND_MESSAGE)
      traceback.print_exc()

  def MaybeSendEmail(self, fd, force=False, never=False):
    """Print warning and maybe send email; the contents is from file object.

    Arguments:
      fd: a file object that will be closed.
      force: send the mail even if number of emails sent exceed
        basics.opt_email_bound 
    """
    fd.seek(0, 0)
    Debug(DEBUG_WARNING, "%s", fd.read())
    self.TryToSend(fd, force, never)
    fd.close()


def _RemoveDirectoryTree(tree_top):
  """Recursively remove everything.

  Ignore filesystem errors, because this function may be called as a last resort
  and it does its job on a best-effort basis.
  """
  # Copied, more or less, from Python 2.4 Library Reference.
  if not os.access(tree_top, os.W_OK):
    return
  for root, dirs, files in os.walk(tree_top, topdown=False):
    for name in files:
      try:
        os.remove(os.path.join(root, name))
      except (IOError, OSError):  # should not happen
        pass
    for name in dirs:
      try:
        if os.path.islink(os.path.join(root, name)):
          os.remove(os.path.join(root, name))
        else:
          os.rmdir(os.path.join(root, name))
      except (IOError, OSError):  # should not happen
          pass
    try:
      os.rmdir(root)
    except (IOError, OSError):  # should not happen
      pass


def _CleanOutClientRoots(client_root):
  """Delete client root directory and everything below, for all generations.
  Argument:
    client_root: a directory path ending in "*distcc-*-*"
  """
  # Determine all generations of this directory.
  hyphen_ultimate_position = client_root.rfind('-')
  client_roots = glob.glob("%s-*" % client_root[:hyphen_ultimate_position])
  assert client_root in client_roots, (client_root, client_roots)
  for client_root_ in client_roots:
    _RemoveDirectoryTree(client_root_)


def _CleanOutOthers():
  """Search for left-overs from include servers that have passed away."""
  # Find all distcc-pump directories whether abandoned or not.
  distcc_directories = glob.glob("%s/*.%s-*-*" % (basics.client_tmp,
                                                   basics.INCLUDE_SERVER_NAME))
  for directory in distcc_directories:
    # Fish out pid from end of directory name.
    hyphen_ultimate_position = directory.rfind('-')
    assert hyphen_ultimate_position != -1
    hyphen_penultimate_position = directory[:hyphen_ultimate_position].rfind(
        '-')
    assert hyphen_penultimate_position != -1
    pid_str = directory[hyphen_penultimate_position + 1:
                        hyphen_ultimate_position]
    try:
      pid = int(pid_str)
    except ValueError:
      continue  # Happens only if a spoofer is around.
    try:
      # Got a pid; does it still exist?
      os.getpgid(pid)
      continue
    except OSError:
      # Process pid does not exist. Nuke its associated files. This will
      # of course only succeed if the files belong the current uid of
      # this process.
      if not os.access(directory, os.W_OK):
        continue  # no access, not ours
      Debug(DEBUG_TRACE,
            "Cleaning out '%s' after defunct include server." % directory)
      _CleanOutClientRoots(directory)


NEWLINE_RE = re.compile(r"\n", re.MULTILINE)
BACKSLASH_NEWLINE_RE = re.compile(r"\\\n", re.MULTILINE)


def ExactDependencies(cmd, realpath_map, systemdir_prefix_cache,
                      translation_unit):
  """The dependencies as calculated by CPP, the C Preprocessor.
  Arguments:
    cmd:  the compilation command, a string
    realpath_map: map from filesystem paths (no symlink) to idx
    systemdir_prefix_cache: says whether realpath starts with a systemdir
    translation_unit: string
  Returns:
    the set of realpath indices of the include dependencies.
  Raises:
    NotCoveredError
  """

  # Safely get a couple of temporary files.
  (fd_o, name_o) = tempfile.mkstemp("distcc-pump")
  (fd_d, name_d) = tempfile.mkstemp("distcc-pump")

  def _delete_temp_files():
    os.close(fd_d)
    os.close(fd_o)
    os.unlink(name_o)
    os.unlink(name_d)

  # Remove -o option and call with -E, -M, and -MF flags.
  preprocessing_command = (
    (re.sub(r"\s-o[ ]?(\w|[./+-])+", " ", cmd) # delete -o option
     + " -o %(name_o)s"                        # add it back, but to temp file,
     + " -E"                                   # macro processing only
     + " -M -MF %(name_d)s") %                  # output .d file
    {'name_o':name_o, 'name_d':name_d})

  ret = os.system(preprocessing_command)
  if ret:
    _delete_temp_files()
    raise NotCoveredError("Could not execute '%s'" %
                          preprocessing_command,
                          translation_unit)
  # Using the primitive fd_d file descriptor for reading is cumbersome, so open
  # normally as well.
  fd_d_ = open(name_d, "rb")
  # Massage the contents of fd_d_
  dotd = re.sub("^.*:", # remove Makefile target
                "",
                NEWLINE_RE.sub(
                  "", # remove newlines
                  BACKSLASH_NEWLINE_RE.sub("", # remove backslashes
                                   fd_d_.read())))
  fd_d_.close()
  _delete_temp_files()
  # The sets of dependencies is a set the of realpath indices of the 
  # absolute filenames corresponding to files in the dotd file.
  deps = set([ rp_idx
               for filepath in dotd.split()
               for rp_idx in [ realpath_map.Index(os.path.join(os.getcwd(),
                                                               filepath)) ]
               if not systemdir_prefix_cache.StartsWithSystemdir(rp_idx,
                                                                 realpath_map)
              ])
  statistics.len_exact_closure = len(deps)
  return deps


def WriteDependencies(deps, result_file, realpath_map):
  """Write the list of deps to result_file.
  Arguments:
    deps: a list of realpath indices
    result_file: a filepath
    realpath_map: map from filesystem paths (no symlink) to idx
  """
  try:
    fd = open(result_file, "w")
    fd.write("\n".join([realpath_map.string[d] for d in deps]))
    fd.write("\n")
    fd.close()
  except (IOError, OSError), why:
    raise NotCoveredError("Could not write to '%s': %s" % (result_file, why))


def VerifyExactDependencies(include_closure,
                            exact_no_system_header_dependency_set,
                            realpath_map,
                            translation_unit):
  """Compare computed and real include closures, ignoring system
  header files (such as those in /usr/include).
  Arguments:
    include_closure: a dictionary whose keys are realpath indices
    exact_no_system_header_dependency_set: set of realpath indices
    realpath_map: map from filesystem paths (no symlink) to idx
    translation_unit: string
  Raises:
    NotCoveredError
"""
  diff = exact_no_system_header_dependency_set - set(include_closure)
  statistics.len_surplus_nonsys = (
    len(set(include_closure) - exact_no_system_header_dependency_set))

  if diff != set([]):
    # Pick one bad dependency.
    bad_dep = diff.pop()
    raise NotCoveredError(
      ("Calculated include closure does not contain: '%s'.\n"
       + "There %s %d such missing %s.")
      % (realpath_map.string[bad_dep],
         len(diff) == 0 and "is" or "are",
         len(diff) + 1,
         len(diff) == 0 and "dependency" or "dependencies"),
      translation_unit)


# A SOCKET SERVER

class QueuingSocketServer(SocketServer.UnixStreamServer):
  """A socket server whose request queue have size REQUEST_QUEUE_SIZE."""
  request_queue_size = REQUEST_QUEUE_SIZE

  def handle_error(self, _, client_address):
    """Re-raise current exception; overrides SocketServer.handle_error.
    """
    raise


# HANDLER FOR SOCKETSERVER

def DistccIncludeHandlerGenerator(include_analyzer):
  """Wrap a socketserver based on the include_analyzer object inside a new
  type that is a class named IncludeHandler."""

  # TODO(klarlund): Can we do this without dynamic type generation?

  class IncludeHandler(SocketServer.StreamRequestHandler):
    """Define a handle() method that invokes the include closure algorithm ."""

    def handle(self):
      """Using distcc protocol, read command and return include closure.

      Do the following:
       - Read from the socket, using the RPC protocol of distcc:
          - the current directory, and
          - the compilation command, already broken down into an argv vector.
       - Parse the command to find options like -I, -iquote,...
       - Invoke the include server's closure algorithm to yield a set of files
         and set of symbolic links --- both sets of files under client_root,
         which duplicates the part of the file system that CPP will need.
       - Transmit the file and link names on the socket using the RPC protocol.
      """
      statistics.StartTiming()
      currdir = distcc_pump_c_extensions.RCwd(self.rfile.fileno())
      cmd = distcc_pump_c_extensions.RArgv(self.rfile.fileno())

      try:
        try:
          # We do timeout the include_analyzer using the crude mechanism of
          # SIGALRM. This signal is problematic if raised while Python is doing
          # I/O in the C extensions and during use of the subprocess
          # module.
          #
          # TODO(klarlund) The Python library manual states: "When a signal
          # arrives during an I/O operation, it is possible that the I/O
          # operation raises an exception after the signal handler returns. This
          # is dependent on the underlying Unix system's semantics regarding
          # interrupted system calls."  We must clarify this. Currently, there
          # is I/O during DoCompilationCommand:
          #
          #  - when a link is created in mirror_path.py
          #  - module compress_files is used
          #
          # TODO(klarlund): Modify mirror_path so that is accumulates symbolic
          # link operations instead of actually executing them on the spot. The
          # accumulated operations can be executed after DoCompilationCommand
          # when the timer has been cancelled.
          include_analyzer.timer = basics.IncludeAnalyzerTimer()
          files_and_links = include_analyzer.DoCompilationCommand(cmd, currdir)
        finally:
          # The timer should normally be cancelled during normal execution
          # flow. Still, we want to make sure that this is indeed the case in
          # all circumstances.
          include_analyzer.timer.Cancel()

      except NotCoveredError, inst:
        # Warn user. The 'Preprocessing locally' message is meant to
        # assure the user that the build process is otherwise intact.
        fd = os.tmpfile()
        print >> fd, (
          "Preprocessing locally. Include server not covering: %s for "
          + "translation unit '%s'") % (
            (inst.args and inst.args[0] or "unknown reason",
             include_analyzer.translation_unit)),
        # We don't include a stack trace here.
        include_analyzer.email_sender.MaybeSendEmail(fd,
                                                     never=not inst.send_email)
        # The empty argv list denotes failure. Communicate this
        # information back to the distcc client, so that it can fall
        # back to preprocessing on the client.
        distcc_pump_c_extensions.XArgv(self.wfile.fileno(), [])
        if isinstance(inst, NotCoveredTimeOutError):
          Debug(DEBUG_TRACE,
                "Clearing caches because of include server timeout.")
          include_analyzer.ClearStatCaches()
        
      except SignalSIGTERM:
        # Normally, we will get this exception when the include server is no
        # longer needed. But we also handle it here, during the servicing of a
        # request. See basics.RaiseSignalSIGTERM.
        Debug(DEBUG_TRACE, "SIGTERM received while handling request.")
        raise
      except KeyboardInterrupt:
        # Propagate to the last-chance exception handler in Main.
        raise
      except SystemExit, inst:
        # When handler tries to exit (by invoking sys.exit, which in turn raises
        # SystemExit), something is really wrong. Terminate the include
        # server. But, print out an informative message first.
        fd = os.tmpfile()
        print >> fd, (
          ("Preprocessing locally. Include server fatal error: '%s' for "
           + "translation unit '%s'") % (
          (inst.args, include_analyzer.translation_unit))),
        _PrintStackTrace(fd)
        include_analyzer.email_sender.MaybeSendEmail(fd, force=True)
        distcc_pump_c_extensions.XArgv(self.wfile.fileno(), [])
        sys.exit("Now terminating include server.")
      # All other exceptions are trapped here.
      except Exception, inst:
        # Internal error. Better be safe than sorry: terminate include
        # server. But show error to user on stderr. We hope this message will be
        # reported.
        fd = os.tmpfile()
        print >> fd, (
          ("Preprocessing locally. Include server internal error: '%s: %s' "
           + "for translation unit '%s'") % (
          (inst.__class__, inst.args, include_analyzer.translation_unit))),
        _PrintStackTrace(fd)
        # Force this email through (if basics.opt_send_email is True), because
        # this is the last one and this is an important case to report.
        include_analyzer.email_sender.MaybeSendEmail(fd, force=True)
        distcc_pump_c_extensions.XArgv(self.wfile.fileno(), [])
        raise SignalSIGTERM  # to be caught in Main with no further stack trace
      else:
        # No exception raised, include closure can be trusted.
        distcc_pump_c_extensions.XArgv(self.wfile.fileno(), files_and_links)
      # Finally, stop the clock and report statistics if needed.
      statistics.EndTiming()
      if basics.opt_statistics:
        statistics.PrintStatistics(include_analyzer)


  return IncludeHandler


def _ParseCommandLineOptions():
  """Parse arguments and options for the include server command.

  Returns:
    (include_server_port, pid_file), where include_server_port
    is a string and pid_file is a string or None 
  Modifies:
    option variables in module basics
  """
  try:
    opts, args = getopt.getopt(sys.argv[1:],
			       "d:estvw",
			       ["port=",
                                "pid_file=",
                                "debug_pattern=",
                                "email",
                                "no-email",
                                "email_bound=",
                                "exact_analysis",
                                "stat_reset_triggers=",
                                "simple_algorithm",
                                "realpath_warning_re=",
                                "statistics",
                                "time",
                                "verify",
                                "write_include_closure"])
  except getopt.GetoptError:
    # Print help information and exit.
    Usage()
    sys.exit(1)
  pid_file = None
  include_server_port = None
  for opt, arg in opts:
    try:
      if opt in ("-d", "--debug_pattern"):
	basics.opt_debug_pattern = int(arg)
      if opt in ("--port",):
        include_server_port = arg
      if opt in ("--pid_file",):
        pid_file = arg
      if opt in ("-e", "--email"):
        basics.opt_send_email = True
      if opt in ("--no-email",):
        basics.opt_send_email = False
      if opt in ("--email_bound",):
        basics.opt_email_bound = int(arg)
      if opt in ("--realpath_warning_re",):
        basics.opt_realpath_warning_re = re.compile(arg)
      if opt in ("--stat_reset_triggers",):
        basics.opt_stat_reset_triggers = (
          dict([ (glob_expr,
                  dict ([ (path, basics.Stamp(path))
                          for path in glob.glob(glob_expr) ]))
                 for glob_expr in arg.split(':') ]))
      if opt in ("--simple_algorithm",):
        basics.opt_simple_algorithm = True
        sys.exit("Not implemented")
      if opt in ("-s", "--statistics"):
        basics.opt_statistics = True
      if opt in ("-t", "--time"):
        basics.opt_print_times = True
      if opt in ("-v", "--verify"):
        basics.opt_verify = True
      if opt in ("-w", "--write_include_closure"):
        basics.opt_write_include_closure = True
      if opt in ("-x", "--exact_analysis"):
        basics.opt_exact_include_analysis = True
    except ValueError:
      Usage()
      sys.exit(1)
  # We must have a port!
  if not include_server_port:
    print >> sys.stderr, "INCLUDE_SERVER_PORT not provided. Aborting."
    print >> sys.stderr, "-------------------------------------------"
    print >> sys.stderr
    Usage()
    sys.exit(1)
  return (include_server_port, pid_file)


def _PrintTimes(times_at_start, times_at_fork, times_child):
  """Print elapsed, user, system, and user + system times."""
  # The os.times format stores user time in positions 0 and 2 (for parent and
  # children, resp.) Similarly, system time is stored in positions 1 and
  # 3. Elapsed time is in position 4. Elapsed time is measured relative to some
  # epoch whereas user and system time are 0 at the time of process creation.
  total_u = (times_at_fork[0] + times_at_fork[2]
             + times_child[0] + times_child[2])
  total_s = (times_at_fork[1] + times_at_fork[3]
             + times_child[1] + times_child[1])
  total_cpu = total_u + total_s
  total_e = times_child[4] - times_at_start[4]
  print >> sys.stderr, "Include server timing. ",
  print >> sys.stderr, (
    "Elapsed: %3.1fs User: %3.1fs System: %3.1fs User + System: %3.1fs" % 
    (total_e, total_u, total_s, total_cpu))


class _IncludeServerPortReady(object):
  """A simple semaphore for forked processes.

   The implementation uses an unnamed pipe."""

  def __init__(self):
    """Constructor.

    Should be called before fork.
    """
    (self.read_fd, self.write_fd) = os.pipe()

  def Acquire(self):
    """Acquire the semaphore after fork;  blocks until a call of Release."""
    if os.read(self.read_fd, 1) != '\n':
      sys.exit("Include server: _IncludeServerPortReady.Acquire failed.")

  def Release(self):
    """Release the semaphore after fork."""
    if os.write(self.write_fd, '\n') != 1:
      sys.exit("Include server: _IncludeServerPortReady.Release failed.")


def _SetUp(include_server_port):
  """Setup include_analyzer and socket server.

  Returns: (include_analyzer, server)"""
  
  try:
    os.unlink(include_server_port)
  except (IOError, OSError):
    pass  # this would be expected, the port provided should not exist

  if os.sep != '/':
    sys.exit("Expected '/' as separator in filepaths.")

  # Determine basics.client_tmp now.
  basics.InitializeClientTmp()
  # So that we can call this function --- to sweep out possible junk. Also, this
  # will allow the include analyzer to call InitializeClientRoot.
  _CleanOutOthers()

  Debug(DEBUG_TRACE, "Starting socketserver %s" % include_server_port)

  # Create the analyser.
  include_analyzer = (
      include_analyzer_memoizing_node.IncludeAnalyzerMemoizingNode(
        basics.opt_stat_reset_triggers))
  include_analyzer.email_sender = _EmailSender()
  # Wrap it inside a handler that is a part of a UnixStreamServer.
  server = QueuingSocketServer(
    include_server_port,
    # Now, produce a StreamRequestHandler subclass whose new objects has
    # a handler which calls the include_analyzer just made.
    DistccIncludeHandlerGenerator(include_analyzer))

  return (include_analyzer, server)


def _CleanOut(include_analyzer, include_server_port):
  """Prepare shutdown by cleaning out files and unlinking port."""
  if include_analyzer and include_analyzer.client_root:
    _CleanOutClientRoots(include_analyzer.client_root)
  try:
    os.unlink(include_server_port)
  except OSError:
    pass


def Main():
  """Parse command line, fork, and start stream request handler."""
  # Remember the time spent in the parent.
  times_at_start = os.times()
  include_server_port, pid_file = _ParseCommandLineOptions()
  # Get locking mechanism.
  include_server_port_ready = _IncludeServerPortReady()
  # Now spawn child so that parent can exit immediately after writing
  # the process id of child to the pid file.
  times_at_fork = os.times()
  pid = os.fork()
  if pid != 0: 
    # In parent.
    #
    if pid_file:
      pid_file_fd = open(pid_file, "w")
      print >> pid_file_fd, pid
      pid_file_fd.close()
    # Just run to completion now -- after making sure that child is ready.
    include_server_port_ready.Acquire()
    # concerned.
  else:
    # In child.
    #
    # We call _Setup only now, because the process id, used in naming the client
    # root, must be that of this process, not that of the parent process. See
    # _CleanOutOthers for the importance of the process id.
    (include_analyzer, server) = _SetUp(include_server_port)
    include_server_port_ready.Release()
    try:
      try:
        server.serve_forever()
      except KeyboardInterrupt:
        print >> sys.stderr, (
            "Include server: keyboard interrupt, quitting after cleaning up.")
        _CleanOut(include_analyzer, include_server_port)
      except SignalSIGTERM:
        Debug(DEBUG_TRACE, "Include server shutting down.")
        _CleanOut(include_analyzer, include_server_port)
      except:
        print >> sys.stderr, (
            "Include server: exception occurred, quitting after cleaning up.")
        _PrintStackTrace(sys.stderr)
        _CleanOut(include_analyzer, include_server_port)
        raise # reraise exception
    finally:
      if basics.opt_print_times:
        _PrintTimes(times_at_start, times_at_fork, os.times())

        
if __name__ == "__main__":
  # Treat SIGTERM (the default of kill) as Ctrl-C.  
  signal.signal(signal.SIGTERM, basics.RaiseSignalSIGTERM)
  Main()