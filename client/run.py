#!/usr/bin/env python

# Import from standard Python library
import os.path
import sys
import argparse

# Import third party libraries
import numpy

# Import personal custom stuff
import optimize
import job
import report

def main():
    parser = argparse.ArgumentParser()
#KB    parser.add_argument('xmlfile',                 type=file, help='XML formatted configuration file')
    parser.add_argument('xmlfile',                 type=str, help='XML formatted configuration file')
    parser.add_argument('-m', '--method',          type=str, choices=('DE', 'fmin', 'galileo'), help='Optimization method: DE = Differential Evolution genetic algorithm, fmin = Nelder-Mead simplex, galileo = galileo genetic algorithm')
    parser.add_argument('-t', '--transport',       type=str, choices=('http', 'mysql'), help='Transport to use for server communication: http or mysql')
    parser.add_argument('-r', '--reportfrequency', type=int,    help='Time between result reports (seconds).')
    parser.add_argument('-i', '--interactive',     action='store_true', help='Whether to allow for user interaction (input from stdin) when making decisions')
    parser.add_argument('-n', '--ncpus',           type=int,    help='Number of CPUs to use (only for Differential Evolution genetic algorithm).')
    parser.add_argument('--tempdir',               type=str, help='Temporary directory for GOTM setups.')
    parser.add_argument('--ppservers',             type=str, help='Comma-separated list of names/IPs of Parallel Python servers to run on (only for Differential Evolution genetic algorithm).')
    parser.set_defaults(method='DE', transport=None, interactive=False, ncpus=None, ppservers=None, reportfrequency=None, tempdir=None, scenarios='.')
    args = parser.parse_args()

    allowedtransports = None
    if args.transport is not None:
        allowedtransports = (args.transport,)

    ppservers = ()
    if args.ppservers is not None:
        ppservers = tuple(args.ppservers.split(','))

    print 'Reading configuration from %s...' % args.xmlfile
    current_job = job.fromConfigurationFile(args.xmlfile, tempdir=args.tempdir)

    with open(args.xmlfile) as f:
        xml = f.read()
    reporter = report.fromConfigurationFile(args.xmlfile, xml, allowedtransports=allowedtransports)

    # Configure result reporter
    reporter.interactive = args.interactive
    if args.reportfrequency is not None:
        reporter.timebetweenreports = args.reportfrequency

    opt = optimize.Optimizer(current_job, reportfunction=reporter.reportResult)

    repeat = True
    while repeat:
        repeat = args.method != 'fmin'   # repeating is only useful for stochastic algorithms - not for deterministic ones

        logtransform = current_job.getParameterLogScale()
        if args.method == 'fmin':
            vals = opt.run(method=optimize.SIMPLEX, par_ini=current_job.createParameterSet(), logtransform=logtransform)
        elif args.method == 'DE':
            minpar, maxpar = current_job.getParameterBounds()

            popsize = 10*len(minpar)
            maxgen = 4000
            startpoppath = 'startpop.dat'

            startpop = None
            if os.path.isfile(startpoppath):
                # Retrieve cached copy of the observations
                print 'Reading initial population from file %s...' % startpoppath
                startpop = numpy.load(startpoppath)

            # parameterCount, populationSize, maxGenerations, minInitialValue, maxInitialValue, deStrategy, diffScale, crossoverProb, cutoffEnergy, useClassRandomNumberMethods, polishTheBestTrials
            vals = opt.run(method=optimize.DIFFERENTIALEVOLUTION, par_min=minpar, par_max=maxpar, popsize=popsize, maxgen=maxgen, F=0.5, CR=0.9, initialpopulation=startpop, ncpus=args.ncpus, ppservers=ppservers, modules=('run',), logtransform=logtransform)

            #print 'Generation %i done. Current best fitness = %.6g.' % (itn,P.maxFitness)

        print 'Best parameter set:'
        for parameter, value in zip(current_job.parameters, vals):
            print '  %s = %.6g' % (parameter.name, value)

    reporter.finalize()

if __name__ == '__main__':
    main()
