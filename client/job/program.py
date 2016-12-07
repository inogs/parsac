# Import from standard Python library (>= 2.4)
import sys
import os.path
import re
import datetime
import time
import tempfile
import atexit
import shutil
import subprocess
import cPickle
import hashlib

# Import third-party modules
import numpy
import netCDF4

try:
    import yaml
except ImportError:
    yaml = None

# Import custom packages
import namelist
import shared

IDLE_PRIORITY_CLASS = 0x00000040

# Regular expression for GOTM datetimes
datetimere = re.compile(r'(\d\d\d\d).(\d\d).(\d\d) (\d\d).(\d\d).(\d\d)\s*')

# Determine if we are running on Windows
windows = sys.platform == 'win32'

def writeNamelistFile(path, nmls, nmlorder):
    with open(path, 'w') as f:
        for nml in nmlorder:
            f.write('&%s\n' % nml)
            for name, value in nmls[nml].iteritems():
                f.write('\t%s = %s,\n' % (name, value))
            f.write('/\n\n')

# Generic parse routine for namelist file
# Returns dictionary linking namelist names to namelist data
# (another dictionary linking parameter names to parameter values)
def parseNamelistFile(path):
    nmls, nmlorder = {}, []
    nmlfile = namelist.NamelistFile(path)
    while 1:
        try:
            nml = nmlfile.parseNextNamelist()
        except:
            break
        nmls[nml.name] = dict(nml)
        nmlorder.append(nml.name)
    return nmls, tuple(nmlorder)

def getMD5(path):
    #print 'Calculating MD5 hash of %s...' % path
    with open(path, 'rb') as f:
        m = hashlib.md5()
        while 1:
            block = f.read(m.block_size)
            if not block: break
            m.update(block)
    return m.digest()

class NamelistParameter(shared.Parameter):
    def __init__(self, job, att):
        self.file = os.path.normpath(att.get('file', unicode))
        self.namelist = att.get('namelist', unicode)
        self.variable = att.get('variable', unicode)
        shared.Parameter.__init__(self, job, att, name='%s/%s/%s' % (self.file, self.namelist, self.variable))

        if not hasattr(job, 'namelistfiles'):
            job.namelistfiles, job.namelistorder = {}, {}

        self.own_file = self.file not in job.namelistfiles 
        if self.own_file:
            # Read all namelist in the file, and store their data and order.
            job.namelistfiles[self.file], job.namelistorder[self.file] = parseNamelistFile(os.path.join(job.scenariodir, self.file))

        if self.namelist not in job.namelistfiles[self.file]:
            raise Exception('Namelist "%s" is not present in "%s".' % (self.namelist, self.file))
        self.namelist_data = job.namelistfiles[self.file][self.namelist]
        if self.variable not in self.namelist_data:
            raise Exception('Variable "%s" is not present in namelist %s in "%s".' % (self.variable, self.namelist, self.file))

    def initialize(self):
        # Update path to namelist file to match temporary scenario directory.
        self.path = os.path.join(self.job.scenariodir, self.file)

        # If we already read this namelist file for some other parameter, just continue.
        if self.own_file:
            # Backup current namelist file
            icopy = 0
            while os.path.isfile(self.path+'.backup%02i' % icopy):
                icopy += 1
            shutil.copy(self.path, self.path+'.backup%02i' % icopy)

    def setValue(self, value):
        self.namelist_data[self.variable] = '%.15g' % value

    def store(self):
        if self.own_file:
            with open(self.path, 'w') as f:
                writeNamelistFile(self.path, self.job.namelistfiles[self.file], self.job.namelistorder[self.file])

class YamlParameter(shared.Parameter):
    def __init__(self, job, att):
        self.file = os.path.normpath(att.get('file', unicode))
        self.variable = att.get('variable', unicode)
        if yaml is None:
            raise Exception('Unable to handle parameter %s/%s because pyyaml package is not available.' % (self.file, self.variable)) 
        shared.Parameter.__init__(self, job, att, name='%s/%s' % (self.file, self.variable))

        if not hasattr(job, 'yamlfiles'):
            job.yamlfiles = {}

        self.own_file = self.file not in job.yamlfiles
        if self.own_file:
            # Read all namelist in the file, and store their data and order.
            with open(os.path.join(job.scenariodir, self.file), 'rU') as f:
                job.yamlfiles[self.file] = yaml.load(f)

        self.target_dict = job.yamlfiles[self.file]
        path_comps = self.variable.split('/')
        for i, comp in enumerate(path_comps[:-1]):
            if comp not in self.target_dict:
                raise Exception('Variable "%s" not found in "%s" (key "%s" not found below /%s)' % (self.variable, self.file, comp, '/'.join(path_comps[:i])))
            self.target_dict = self.target_dict[comp]
        self.name = path_comps[-1]
        if self.name not in self.target_dict:
            raise Exception('Variable "%s" not found in "%s" (key "%s" not found below /%s)' % (self.variable, self.file, self.name, '/'.join(path_comps[:-1])))

    def initialize(self):
        # Update path to namelist file to match temporary scenario directory.
        self.path = os.path.join(self.job.scenariodir, self.file)

        # If we already read this yaml file for some other parameter, just continue.
        if self.own_file:
            # Backup current namelist file
            icopy = 0
            while os.path.isfile(self.path+'.backup%02i' % icopy):
                icopy += 1
            shutil.copy(self.path, self.path+'.backup%02i' % icopy)

    def setValue(self, value):
        self.target_dict[self.name] = float(value)

    def store(self):
        if self.own_file:
            with open(self.path, 'w') as f:
                yaml.dump(self.job.yamlfiles[self.file], f, default_flow_style=False)

class Job(shared.Job):
    verbose = True

    def __init__(self, job_id, xml_tree, root, copyexe=False, tempdir=None, simulationdir=None):
        # Allow overwrite of setup directory (default: directory with xml configuration file)
        element = xml_tree.find('setup')
        if element is not None:
            att = shared.XMLAttributes(element, 'the setup element')
            self.scenariodir = os.path.join(root, att.get('path', unicode))
            att.testEmpty()
        else:
            self.scenariodir = root

        # Get executable path
        element = xml_tree.find('executable')
        if element is None:
            raise Exception('The root node must contain a single "executable" element.')
        att = shared.XMLAttributes(element, 'the executable element')
        self.exe = os.path.realpath(os.path.join(root, att.get('path', unicode)))
        att.testEmpty()

        self.simulationdir = simulationdir

        if tempdir is not None:
            tempdir = os.path.abspath(tempdir)
        self.tempdir = tempdir

        if copyexe is None:
            copyexe = not hasattr(sys, 'frozen')
        self.copyexe = copyexe

        #self.controller = gotmcontroller.Controller(scenariodir, path_exe, copyexe=not hasattr(sys, 'frozen'), tempdir=tempdir, simulationdir=simulationdir)

        # Initialize base class
        shared.Job.__init__(self, job_id, xml_tree, root)

        # Array to hold observation datasets
        self.observations = []

        # Whether to reject parameter sets outside of initial parameter ranges.
        # Rejection means returning a ln likelihood of negative infinity.
        self.checkparameterranges = True

        # Parse observations section
        n = 0
        for iobs, element in enumerate(xml_tree.findall('observations/variable')):
            att = shared.XMLAttributes(element, 'observed variable %i' % (iobs+1))
            source = att.get('source', unicode)
            att.description = 'observation set %s' % source
            sourcepath = os.path.normpath(os.path.join(root, source))
            assert os.path.isfile(sourcepath), 'Observation source file "%s" does not exist.' % sourcepath
            modelvariable = att.get('modelvariable', unicode)
            modelpath = att.get('modelpath', unicode)
            file_format = att.get('format', unicode, default='profiles')
            n += self.addObservation(sourcepath, modelvariable, modelpath,
                                     maxdepth          =att.get('maxdepth',           float, required=False, minimum=0.),
                                     mindepth          =att.get('mindepth',           float, required=False, minimum=0.),
                                     spinupyears       =att.get('spinupyears',        int, required=False, minimum=0),
                                     logscale          =att.get('logscale',           bool, default=False),
                                     relativefit       =att.get('relativefit',        bool, default=False),
                                     min_scale_factor  =att.get('minscalefactor',     float, required=False),
                                     max_scale_factor  =att.get('maxscalefactor',     float, required=False),
                                     fixed_scale_factor=att.get('constantscalefactor',float, required=False),
                                     minimum           =att.get('minimum',            float, default=0.1),
                                     sd                =att.get('sd',                 float, required=False, minimum=0.),
                                     file_format       ={'profiles':0, 'timeseries':1}[file_format],
                                     cache=True)
            att.testEmpty()
        if n == 0:
            raise Exception('No valid observations found within specified depth and time range.')

    def getParameter(self, att):
        if att.get('dummy', bool, default=False):
            return shared.DummyParameter(self, att)
        strfile = att.get('file', unicode, required=False)
        if strfile.endswith('.yaml'):
            return YamlParameter(self, att)
        return NamelistParameter(self, att)

    def getSimulationStart(self):
        raise NotImplementedError()

    def addObservation(self, observeddata, outputvariable, outputpath, spinupyears=None, relativefit=False, min_scale_factor=None, max_scale_factor=None, sd=None, maxdepth=None, mindepth=None, cache=True, fixed_scale_factor=None, logscale=False, minimum=None, file_format=0):
        sourcepath = None
        if mindepth is None: mindepth = -numpy.inf
        if maxdepth is None: maxdepth = numpy.inf
        if maxdepth < 0: print 'WARNING: maxdepth=%s, but typically should be positive (downward distance from surface in meter).' % maxdepth
        assert maxdepth > mindepth, 'ERROR: maxdepth=%s should be greater than mindepth=%s' % (maxdepth, mindepth)

        assert isinstance(observeddata, basestring), 'Currently observations must be supplied as path to an 3-column ASCII file.'

        # Observations are specified as path to ASCII file.
        sourcepath = observeddata
        md5 = getMD5(sourcepath)

        observeddata = None
        if cache and os.path.isfile(sourcepath+'.cache'):
            # Retrieve cached copy of the observations
            with open(sourcepath+'.cache', 'rb') as f:
                oldmd5 = cPickle.load(f)
                if oldmd5 != md5:
                    print 'Cached copy of %s is out of date - file will be reparsed.' % sourcepath
                else:
                    print 'Loading cached copy of %s...' % sourcepath
                    observeddata = cPickle.load(f)

        if not isinstance(observeddata, tuple):
            # Parse ASCII file and store observations as matrix.
            if self.verbose:
                print 'Reading observations for variable "%s" from "%s".' % (outputvariable, sourcepath)
            if not os.path.isfile(sourcepath):
                raise Exception('"%s" is not a file.' % sourcepath)
            times, zs, values = [], [], []
            with open(sourcepath, 'rU') as f:
                for iline, line in enumerate(f):
                    if self.verbose and (iline+1)%20000 == 0:
                        print 'Read "%s" upto line %i.' % (sourcepath, iline)
                    if line.startswith('#'): continue
                    datematch = datetimere.match(line)
                    if datematch is None:
                        raise Exception('Line %i does not start with time (yyyy-mm-dd hh:mm:ss). Line contents: %s' % (iline+1, line))
                    refvals = map(int, datematch.group(1, 2, 3, 4, 5, 6)) # Convert matched strings into integers
                    curtime = datetime.datetime(*refvals)
                    data = line[datematch.end():].rstrip('\n').split()
                    if file_format == 0:
                        if len(data) != 2:
                            raise Exception('Line %i does not contain two values (depth, observation) after the date + time, but %i values.' % (iline+1, len(data)))
                        z = float(data[0])
                        if not numpy.isfinite(z):
                            raise Exception('Depth on line %i is not a valid number: %s.' % (iline+1, data[0]))
                        if -z < mindepth or -z > maxdepth: continue
                        zs.append(z)
                    else:
                        if len(data) != 1:
                            raise Exception('Line %i does not contain one value (observation) after the date + time, but %i values.' % (iline+1, len(data)))
                    times.append(curtime)
                    value = float(data[-1])
                    if not numpy.isfinite(value):
                        raise Exception('Observed value on line %i is not a valid number: %s.' % (iline+1, data[-1]))
                    values.append(value)
            if file_format == 0:
                zs = numpy.array(zs)
            else:
                zs = None
            values = numpy.array(values)

            # Try to store cached copy of observations
            if cache:
                try:
                    with open(sourcepath+'.cache', 'wb') as f:
                        cPickle.dump(md5, f, cPickle.HIGHEST_PROTOCOL)
                        cPickle.dump((times, zs, values), f, cPickle.HIGHEST_PROTOCOL)
                except Exception, e:
                    print 'Unable to store cached copy of observation file. Reason: %s' % e
        else:
            times, zs, values = observeddata

        if logscale and minimum is None:
            raise Exception('For log scale fitting, the (relevant) minimum value must be specified.')

        # Remove observations that lie within spin-up period (or before simulation start).
        if spinupyears is not None:
            start = self.getSimulationStart()
            obsstart = datetime.datetime(start.year+spinupyears, start.month, start.day)
            valid = numpy.array([t >= obsstart for t in times], dtype=bool)
            zs = zs[valid]
            values = values[valid]
            times = [t for t, v in zip(times, valid) if v]

        self.observations.append({'outputvariable': outputvariable,
                                  'outputpath': outputpath,
                                  'times': times,
                                  'zs': zs,
                                  'values': values,
                                  'relativefit': relativefit,
                                  'min_scale_factor': min_scale_factor,
                                  'max_scale_factor': max_scale_factor,
                                  'fixed_scale_factor': fixed_scale_factor,
                                  'sd': sd,
                                  'sourcepath': sourcepath,
                                  'logscale': logscale,
                                  'minimum': minimum})

        return len(values)

    def getObservationPaths(self):
        return [obsinfo['sourcepath'] for obsinfo in self.observations if obsinfo['sourcepath'] is not None]

    def describe(self):
        obs = []
        for obsinfo in self.observations:
            # Copy key attributes of observation (but not the data matrix)
            infocopy = {}
            for key in ('sourcepath', 'outputvariable', 'relativefit', 'min_scale_factor', 'max_scale_factor', 'sd'):
                infocopy[key] = obsinfo[key]

            # Add attributes describing the data matrix
            times, zs, values = obsinfo['times'], obsinfo['zs'], obsinfo['values']
            infocopy['observationcount'] = data.shape[0]
            infocopy['timerange'] = (min(times), max(times))
            infocopy['depthrange'] = (-float(zs.max()), -float(zs.min()))
            infocopy['valuerange'] = (float(values.min()), float(values.max()))
            obs.append(infocopy)
        parameter_info = [parameter.getInfo() for parameter in self.parameters]
        return cPickle.dumps({'parameters':parameter_info, 'observations':obs})

    def initialize(self):
        assert not self.initialized, 'Job has already been initialized.'

        # Check for presence of GOTM executable.
        if not os.path.isfile(self.exe):
            raise Exception('Cannot locate executable at "%s".' % self.exe)

        # Check for presence of custom temporary directory (if set)
        if self.tempdir is not None and not os.path.isdir(self.tempdir):
            raise Exception('Custom temporary directory "%s" does not exist.' % self.tempdir)

        if self.simulationdir is not None:
            # A specific directory in which to simulate has been provided.
            tempscenariodir = os.path.abspath(self.simulationdir)
            if not os.path.isdir(tempscenariodir): os.mkdir(tempscenariodir)
        else:
            # Create a temporary directory for the scenario on disk
            # (decreases runtime compared to network because GOTM can access observations faster)
            tempscenariodir = tempfile.mkdtemp(prefix='gotmopt', dir=self.tempdir)
            atexit.register(shutil.rmtree, tempscenariodir, True)

        print 'Copying files for model setup...'
        for name in os.listdir(self.scenariodir):
            if name.endswith('.nc'):
                print '   skipping %s because it is a NetCDF file' % name
                continue
            srcname = os.path.join(self.scenariodir, name)
            if os.path.isdir(srcname):
                print '   skipping %s because it is a directory' % name
                continue
            dstname = os.path.join(tempscenariodir, name)
            shutil.copy(srcname, dstname)
        self.scenariodir = tempscenariodir

        if self.copyexe:
            print 'Copying %s executable...' % os.path.basename(self.exe)
            dstname = os.path.join(self.scenariodir, os.path.basename(self.exe))
            shutil.copy(self.exe, dstname)
            self.exe = dstname

        for parameter in self.parameters:
            parameter.initialize()

        self.initialized = True

    def evaluateFitness(self, values, return_model_values=False, show_output=False):
        if not self.initialized:
            self.initialize()
            assert self.initialized

        print 'Evaluating fitness with parameter set [%s].' % ','.join(['%.6g' % v for v in values])

        # If required, check whether all parameters are within their respective range.
        if self.checkparameterranges:
            for parameter, value in zip(self.parameters, values):
                if value < parameter.minimum or value > parameter.maximum:
                    errors = 'Parameter %s with value %.6g out of range (%.6g - %.6g), returning ln likelihood of -infinity.' % (parameter.name, value, parameter.minimum, parameter.maximum)
                    return -numpy.Inf

        # Update the value of all untransformed parameters
        for parameter, value in zip(self.parameters, values):
            parameter.setValue(value)

        # Update namelist parameters that are governed by transforms
        #ipar = len(self.parameters)
        #for transform in self.parametertransforms:
        #    ext = transform.getExternalParameters()
        #    basevals = transform.undoTransform(values[ipar:ipar+len(ext)])
        #    for p, value in zip(transform.getOriginalParameters(), basevals):
        #        nmlpath = os.path.join(self.scenariodir, p[0])
        #        #print 'Setting %s/%s/%s to %s.' % (nmlpath,p[1],p[2],value)
        #        self.namelistfiles[nmlpath][p[1]][p[2]] = '%.15g' % value
        #    ipar += len(ext)

        # Save updated namelist/YAML files.
        for parameter in self.parameters:
            parameter.store()

        returncode = self.run(values, show_output=show_output)

        if returncode != 0:
            # Run failed
            print 'Returning ln likelihood = negative infinity to discourage use of this parameter set.'
            #self.reportResult(values, None, error='Run stopped prematurely')
            return -numpy.Inf

        resultroot = self.scenariodir

        # Check if this is the first model run/evaluation of the likelihood.
        if not hasattr(self, 'file2variables'):
            # This is the first time that we evaluate the likelihood.
            # Find a list of all NetCDF variables that we need.
            # Also find the coordinates in the result arrays and the weights that should be
            # used to interpolate to the observations.

            self.file2variables = {}
            file2re = {}
            for obsinfo in self.observations:
                obsvar, outputpath = obsinfo['outputvariable'], obsinfo['outputpath']
                if not os.path.isfile(os.path.join(resultroot, outputpath)):
                    raise Exception('Output file "%s"" was not created.' % os.path.join(resultroot, outputpath))
                with netCDF4.Dataset(os.path.join(resultroot, outputpath)) as nc:
                    if outputpath not in file2re:
                        file2re[outputpath] = re.compile(r'(?<!\w)('+'|'.join(nc.variables.keys())+r')(?!\w)')  # variable name that is not preceded and followed by a "word" character
                    if outputpath not in self.file2variables:
                        self.file2variables[outputpath] = set()

                    if obsinfo['zs'] is not None:
                        self.file2variables[outputpath].add('h') # always include cell thickness "h"

                    # Find variable names in expression.
                    curncvars = set(file2re[outputpath].findall(obsvar))
                    assert len(curncvars) > 0, 'No variables in found in NetCDF file %s that match %s.' % (outputpath, obsvar)
                    self.file2variables[outputpath] |= curncvars

                    # Check dimensions of all used NetCDF variables
                    firstvar, dimnames = None, None
                    for varname in curncvars:
                        curdimnames = tuple(nc.variables[varname].dimensions)
                        if dimnames is None:
                            firstvar, dimnames = varname, curdimnames
                            assert dimnames[0] == 'time', 'Dimension 1 of variable %s must be time, but is "%s".' % (varname, dimnames[0])
                            assert dimnames[-2:] == ('lat','lon'), 'Last two dimensions of variable %s must be latitude and longitude, but are "%s".'  % (varname, dimnames[-2:])
                            if obsinfo['zs'] is None:
                                assert len(dimnames) == 3, 'Expected 3 dimensions (time, y, x). "%s" has %i dimensions.' % (varname, len(dimnames))
                            else:
                                assert len(dimnames) == 4, 'Expected 4 dimensions (time, z, y, x). "%s" has %i dimensions.' % (varname, len(dimnames))
                                assert dimnames[1] in ('z', 'z1'), 'Dimension 2 of variable %s must be depth (z or z1), but is "%s".' % (varname, dimnames[1])
                                obsinfo['depth_dimension'] = dimnames[1]
                        else:
                            assert curdimnames == dimnames, 'Dimensions of %s %s do not match dimensions of %s %s. Cannot combine both in one expression.' % (varname, curdimnames, firstvar, dimnames)

                    # Get time indices (for left side of bracket for linear interpolation)
                    # This also eliminates points outside the simulated period.
                    print 'Calculating weights for linear interpolation to "%s" observations...' % obsvar,
                    nctime = nc.variables['time']
                    time_vals = nctime[:]
                    if 'itimes' not in obsinfo:
                        itimes_left, iweights_left = [], []
                        valid = numpy.zeros((len(obsinfo['times']),), dtype=bool)
                        numtimes = netCDF4.date2num(obsinfo['times'], nctime.units)
                        for i, numtime in enumerate(numtimes):
                            iright = time_vals.searchsorted(numtime)
                            if iright == 0 or iright >= len(time_vals): continue
                            valid[i] = True
                            itimes_left.append(iright-1)
                            iweights_left.append((numtime-time_vals[iright-1])/(time_vals[iright]-time_vals[iright-1]))
                        obsinfo['times'] = [t for t, v in zip(obsinfo['times'], valid) if v]
                        obsinfo['numtimes'] = numtimes[valid]
                        obsinfo['itimes'] = numpy.array(itimes_left, dtype=int)
                        obsinfo['time_weights'] = numpy.array(iweights_left, dtype=float)
                        obsinfo['values'] = obsinfo['values'][valid]
                        if obsinfo['zs'] is not None:
                            obsinfo['zs'] = obsinfo['zs'][valid]

                    print 'done.'

        # Get all model variables that we need from the NetCDF file.
        file2vardata  = {}
        for path, variables in self.file2variables.items():
           with netCDF4.Dataset(os.path.join(resultroot, path)) as nc:
              file2vardata[path] = dict([(vn, nc.variables[vn][..., 0, 0]) for vn in variables])

        # Start with zero ln likelihood (likelihood of 1)
        lnlikelihood = 0.

        # Enumerate over the sets of observations.
        if return_model_values:
            model_values = []
        for obsinfo in self.observations:
            obsvar, outputpath, obsvals = obsinfo['outputvariable'], obsinfo['outputpath'], obsinfo['values']

            # Get model predictions for current variable or expression.
            all_values_model = eval(obsvar, file2vardata[outputpath])

            if obsinfo['zs'] is not None:
                # Get model depth coordinates (currently expresses depth as distance from current surface elevation!)
                h = file2vardata[outputpath]['h']
                h_cumsum = h.cumsum(axis=1)
                if obsinfo['depth_dimension'] == 'z':
                    # Centres (all)
                    zs_model = h_cumsum - h_cumsum[:, -1, numpy.newaxis] - h/2
                elif obsinfo['depth_dimension'] == 'z1':
                    # Interfaces (all except bottom)
                    zs_model = h_cumsum - h_cumsum[:, -1, numpy.newaxis]

                # Interpolate in depth (extrapolates if required)
                modelvals = numpy.empty_like(obsvals)
                previous_numtime = None
                for i, (numtime, ileft, weight, z) in enumerate(zip(obsinfo['numtimes'], obsinfo['itimes'], obsinfo['time_weights'], obsinfo['zs'])):
                    if previous_numtime != numtime:
                        zprof     = weight*zs_model        [ileft, :] + (1-weight)*zs_model[ileft+1, :]
                        valueprof = weight*all_values_model[ileft, :] + (1-weight)*all_values_model[ileft+1, :]
                        previous_numtime = numtime
                    jright = min(max(1, zprof.searchsorted(z)), len(zprof)-1)
                    z_weight = (z - zprof[jright-1]) / (zprof[jright] - zprof[jright-1])
                    modelvals[i] = (1-z_weight)*valueprof[jright-1] + z_weight*valueprof[jright]
            else:
                modelvals = obsinfo['time_weights']*all_values_model[obsinfo['itimes']] + (1-obsinfo['time_weights'])*all_values_model[obsinfo['itimes']+1]

            if not numpy.isfinite(modelvals).all():
                print 'WARNING: one or more model values for %s are not finite.' % obsvar
                print 'Returning ln likelihood = negative infinity to discourage use of this parameter set.'
                #self.reportResult(values,None,error='Some model values for %s are not finite' % obsvar)
                return -numpy.Inf

            if return_model_values:
                with netCDF4.Dataset(os.path.join(resultroot, outputpath)) as nc:
                    nctime = nc.variables['time']
                    t_centers = nctime[:]
                    time_unit = nctime.units
                if obsinfo['zs'] is not None:
                    if obsinfo['depth_dimension']=='z':
                        # Centres (all)
                        z_interfaces = numpy.hstack((-h_cumsum[:, -1, numpy.newaxis], h_cumsum-h_cumsum[:, -1, numpy.newaxis]))
                    elif obsinfo['depth_dimension']=='z1':
                        # Interfaces (all except bottom)
                        zs_model = h_cumsum-h_cumsum[:, -1, numpy.newaxis]
                    z_interfaces2 = numpy.empty((z_interfaces.shape[0]+1, z_interfaces.shape[1]))
                    delta_z_interfaces = numpy.diff(z_interfaces, axis=0)/2
                    z_interfaces2[0,   :] = z_interfaces[0,  :] - delta_z_interfaces[0,:]
                    z_interfaces2[1:-1,:] = z_interfaces[:-1,:] + delta_z_interfaces
                    z_interfaces2[-1,  :] = z_interfaces[-1, :] + delta_z_interfaces[-1,:]

                    half_delta_time = numpy.diff(t_centers)/2
                    tim_stag = numpy.zeros((len(t_centers)+1,))
                    tim_stag[0 ] = t_centers[0] - half_delta_time[0]
                    tim_stag[1:-1] = t_centers[:-1] + half_delta_time
                    tim_stag[-1] = t_centers[-1] + half_delta_time[-1]
                    t_interfaces = numpy.repeat(netCDF4.num2date(tim_stag, time_unit)[:, numpy.newaxis], z_interfaces2.shape[1], axis=1)
                    model_values.append((t_interfaces, z_interfaces2, all_values_model, modelvals))
                else:
                    model_values.append((netCDF4.num2date(t_centers, time_unit), all_values_model, modelvals))

            if obsinfo['logscale']:
                modelvals = numpy.log10(numpy.maximum(modelvals, obsinfo['minimum']))
                obsvals   = numpy.log10(numpy.maximum(obsvals, obsinfo['minimum']))

            # If the model fit is relative, calculate the optimal model to observation scaling factor.
            scale = None
            if obsinfo['relativefit']:
                if obsinfo['logscale']:
                    # Optimal scale factor is calculated from optimal offset on a log scale.
                    scale = 10.**(obsvals.mean()-modelvals.mean())
                else:
                    # Calculate optimal scale factor.
                    if (modelvals == 0.).all():
                        print 'WARNING: cannot calculate optimal scaling factor for %s because all model values equal zero.' % obsvar
                        print 'Returning ln likelihood = negative infinity to discourage use of this parameter set.'
                        #self.reportResult(values, None, error='All model values for %s equal 0' % obsvar)
                        return -numpy.Inf
                    scale = (obsvals*modelvals).sum()/(modelvals**2).sum()
                    if not numpy.isfinite(scale):
                        print 'WARNING: optimal scaling factor for %s is not finite.' % obsvar
                        print 'Returning ln likelihood = negative infinity to discourage use of this parameter set.'
                        #self.reportResult(values, None, error='Optimal scaling factor for %s is not finite' % obsvar)
                        return -numpy.Inf

                # Report and check optimal scale factor.
                print 'Optimal model-to-observation scaling factor for %s = %.6g.' % (obsvar, scale)
                if obsinfo['min_scale_factor'] is not None and scale < obsinfo['min_scale_factor']:
                    print 'Clipping optimal scale factor to minimum = %.6g.' % obsinfo['min_scale_factor']
                    scale = obsinfo['min_scale_factor']
                elif obsinfo['max_scale_factor'] is not None and scale > obsinfo['max_scale_factor']:
                    print 'Clipping optimal scale factor to maximum = %.6g.' % obsinfo['max_scale_factor']
                    scale = obsinfo['max_scale_factor']
            elif obsinfo['fixed_scale_factor'] is not None:
                scale = obsinfo['fixed_scale_factor']

            # Apply scale factor if set
            if scale is not None:
                if obsinfo['logscale']:
                    modelvals += numpy.log10(scale)
                else:
                    modelvals *= scale

            # Calculate difference between model outcome and observations
            diff = modelvals - obsvals

            # Calculate sum of squares
            ssq = (diff**2).sum()
            n = len(diff)

            # Add to likelihood, weighing according to standard deviation of current data.
            sd = obsinfo['sd']
            if sd is None:
                # No standard deviation specified: calculate the optimal s.d.
                sd = numpy.sqrt(ssq/(n-1))
                print 'Using optimal s.d. for %s = %.6g.' % (obsvar, sd)

            # Note: assuming normally distributed errors, and omitting constant terms in the log likelihood = -n*ln(2*pi)/2
            lnlikelihood += -n*numpy.log(sd)-ssq/2/sd/sd

        print 'ln Likelihood = %.6g.' % lnlikelihood

        if return_model_values: return lnlikelihood, model_values
        return lnlikelihood

    def run(self, values, show_output=False):
        # Take time and start executable
        time_start = time.time()
        print 'Starting model run...'
        args = [self.exe]
        if windows:
            # We start the process with low priority
            if self.exe.endswith('.py'):
                args = [sys.executable] + args
            proc = subprocess.Popen(args, cwd=self.scenariodir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=IDLE_PRIORITY_CLASS)
        else:
            proc = subprocess.Popen(args, cwd=self.scenariodir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        # Simulation is now running
        if show_output:
            while 1:
                line = proc.stdout.readline()
                if line == '': break
                print line,
        proc.communicate()

        # Calculate and show elapsed time. Report error if GOTM did not complete gracefully.
        elapsed = time.time() - time_start
        print 'Model run took %.1f s.' % elapsed
        if proc.returncode != 0:
            print 'WARNING: model returned non-zero code %i - an error must have occured.' % proc.returncode 
        return proc.returncode
