# coding: utf-8
from collections import OrderedDict
import time

import numpy as np
import sympy as sp
import scipy.stats
import scipy.optimize
import pandas as pd

from .feedback import compute_from_transfer_function


# Default frequencies to test
FREQUENCIES = np.logspace(2, np.log10(20e3), 15)

# Nominal capacitor values _(in F)_ for each channel on the feedback test
# board.
TEST_LOADS = pd.Series([1e-12, 1.5e-12, 2.2e-12, 3.3e-12, 4.7e-12, 6.8e-12,
                        1e-11, 1.5e-11, 2.2e-11, 3.3e-11, 4.7e-11, 6.8e-11,
                        1e-10, 1.5e-10, 2.2e-10, 3.3e-10, 4.7e-10, 6.8e-10,
                        1e-9])


def get_test_frame(frequencies, test_loads, n_repeats, n_sampling_windows,
                   actuation_voltage=100):
    '''
    Return a `pandas.DataFrame`, ready to store one measurement per row.
    Upon returning, the following columns are populated:

     - `frequency`: The frequency of the waveform to use during measurement.
     - `test_capacitor`: The test capacitor to use during measurement.
     - `test_channel`: The test capacitor channel to use during measurement.
     - `repeat_index`: The repetition count for the corresponding
       frequency/capacitor measurement set.
     - `sample_index`: The index of the sample in each repetition set.

    The remaining columns are initialized to starting values, but should be
    overwritten by the `run_experiment` function.
    '''
    sample_index = np.tile(range(n_sampling_windows), n_repeats *
                           len(test_loads) * len(frequencies))
    repeat_index = np.tile(np.repeat(range(n_repeats), n_sampling_windows),
                           len(test_loads) * len(frequencies))
    channel = np.tile(np.repeat(test_loads.index.values, n_repeats *
                                n_sampling_windows), len(frequencies))
    capacitor = np.tile(np.repeat(test_loads.values, n_repeats *
                                  n_sampling_windows), len(frequencies))
    frequency = np.repeat(frequencies, len(test_loads) * n_repeats *
                          n_sampling_windows)

    df = pd.DataFrame({'frequency': frequency})
    df['test_capacitor'] = capacitor
    df['test_channel'] = channel
    df['repeat_index'] = repeat_index
    df['sample_index'] = sample_index
    df['V_actuation'] = actuation_voltage
    df['rms'] = True
    df['antialiasing_filter'] = True

    return df


def run_experiment(proxy, test_loads=None, frequencies=None,
                   use_antialiasing_filter=None, rms=None, on_update=None):
    if use_antialiasing_filter is None:
        use_antialiasing_filter = True
    # Update device anti-aliasing setting and reset the device to take effect.
    proxy.use_antialiasing_filter = use_antialiasing_filter
    proxy.connect()

    if test_loads is None:
        test_loads = TEST_LOADS

    if frequencies is None:
        frequencies = FREQUENCIES

    if rms is None:
        rms = True

    # Actuation voltage.
    voltage = 100
    # Number of repeated/independent measurements for each condition.
    n_repeats = 1
    # Number of sampling windows per measurement.
    n_sampling_windows = 10

    # Prepare the test output `pandas.DataFrame`.
    test_frame = get_test_frame(frequencies, test_loads, n_repeats,
                                n_sampling_windows, actuation_voltage=voltage)

    proxy.set_waveform_voltage(voltage)

    previous_frequency = None
    grouped = test_frame.groupby(['frequency', 'test_capacitor',
                                  'test_channel', 'repeat_index'])

    results = []
    for (frequency, C1, channel, i), group in grouped:
        if frequency != previous_frequency:
            proxy.set_waveform_frequency(frequency)
        #print "%.2fkHz, C=%.2fpF, rep=%d" % (frequency / 1e3, 1e12 * C1, i)
        state = np.zeros(proxy.number_of_channels())
        state[channel] = 1
        readings = proxy.measure_impedance(10.0, n_sampling_windows, 0, True,
                                           rms, state)
        data = pd.DataFrame(OrderedDict([('C', readings.capacitance()),
                                         ('V_hv', readings.V_hv),
                                         ('V_fb', readings.V_fb),
                                         ('hv_resistor', readings.hv_resistor),
                                         ('fb_resistor', readings.fb_resistor),
                                         ('sample_index',
                                          range(len(readings.hv_resistor)))]))
        data.set_index('sample_index', inplace=True)
        results.append(data)
        if on_update is not None:
            on_update(frequency, C1, channel, i, data)
    df = pd.concat(results, ignore_index=True)

    calibration = proxy.calibration

    # Set all channels back to zero
    proxy.set_state_of_all_channels(np.zeros(proxy.number_of_channels()))

    return test_frame.join(df), calibration


def fit_fb_calibration(df, calibration):
    '''
    Fit feedback calibration data to solve for values of `C_fb[:]` and
    `R_fb[:]`.

    Returns a `pandas.DataFrame` indexed by the feedback resistor/capacitance
    index, and with the following columns:
     - Model: Either with parasitic capacitance term or not.
     - N: Number of samples used for fit.
     - F: F-value
     - p-value: p-value from Chi squared test.
     - R_fb: Feedback resistor value based on fit.
     - R-CI %: Confidence interval for feedback resistor value.
     - C_fb: Feedback capacitor value based on fit (0 if no-capacitance model
       is used).
     - C-CI %: Confidence interval for feedback capacitance value.

    __N.B.__ This function does not actually _update_ the calibration, it only
    performs the fit.
    See `apply_calibration`.
    '''
    # Set initial guesses for the feedback parameters.
    R_fb = pd.Series([2e2, 2e3, 2e4, 2e5, 2e6])
    C_fb = pd.Series(len(calibration.C_fb) * [50e-12])

    # Error function.
    def error(p0, df, calibration):
        # Impedance of the reference resistor on the HV attenuator circuit.
        Z = 10e6
        R_fb = p0[0]

        # If the parameter vector only contains one variable, the capacitance
        # is zero
        if len(p0) == 2:
            C_fb = p0[1]
        else:
            C_fb = 0

        R_hv = calibration.R_hv[df.hv_resistor.values]
        C_hv = calibration.C_hv[df.hv_resistor.values]

        # Solve feedback transfer function for the actuation voltage, _(i.e.,
        # `V1`)_, based on the high-voltage measurements.
        # Note that the transfer function definition depends on the hardware
        # version.
        V_actuation = compute_from_transfer_function(calibration.hw_version
                                                     .major, 'V1', V2=df.V_hv,
                                                     R1=Z, R2=R_hv, C2=C_hv,
                                                     f=df.frequency)

        # Solve feedback transfer function for the expected impedance feedback
        # voltage, _(i.e., `V2`)_, based on the actuation voltage, the proposed
        # values for `R2` and `C2`, and the reported `C1` value from the
        # feedback measurements.
        # Note that the transfer function definition depends on the hardware
        # version.
        V_impedance = compute_from_transfer_function(calibration.hw_version
                                                     .major, 'V2',
                                                     V1=V_actuation, C1=df.C,
                                                     R2=R_fb, C2=C_fb,
                                                     f=df.frequency)
        return df.V_fb - V_impedance

    # Perform a nonlinear least-squares fit of the data.
    def fit_model(p0, df, calibration):
        p1, cov_x, infodict, mesg, ier = scipy.optimize.leastsq(
            error, p0, args=(df, calibration), full_output=True)
        p1 = np.abs(p1)
        E = error(p1, df, calibration)
        return p1, E, cov_x

    CI = []

    feedback_records = []
    # Fit feedback parameters for each feedback resistor.
    for i in range(len(calibration.R_fb)):
        # Only include data points for the given feedback resistor (and where
        # `hv_resistor` is a valid index).
        df_i = df.loc[(df.fb_resistor == i)].dropna()

        if df_i.shape[0] < 2:
            CI.append([0, 0])
            continue

        # Fit the data assuming no parasitic capacitance (model 1).
        p0_1 = [R_fb[i]]
        p1_1, E_1, cov_x_1 = fit_model(p0_1, df_i, calibration)
        df_1 = (len(E_1) - len(p0_1))
        chi2_1 = np.sum(E_1 ** 2)
        chi2r_1 = chi2_1 / (df_1 - 1)

        # fit the data including parasitic capacitance (model 2)
        p0_2 = [R_fb[i], C_fb[i]]
        p1_2, E_2, cov_x_2 = fit_model(p0_2, df_i, calibration)
        df_2 = (len(E_2) - len(p0_2))
        chi2_2 = np.sum(E_2 ** 2)
        chi2r_2 = chi2_2 / (df_2 - 1)

        # do an F-test to compare the models
        F = (chi2_1 - chi2_2) / chi2r_2
        p_value = scipy.stats.f.cdf(F, 1, df_2-1)

        # if the p_value is > 0.95, we assume that the capacitive term is
        # necessary
        if p_value > .95 and cov_x_2 is not None:
            model = 'w/Parasitic C'
            chi2r = chi2r_2
            R_fb_i = p1_2[0]
            C_fb_i = p1_2[1]
            CI.append((100 * np.sqrt(chi2r_2 * np.diag(cov_x_2)) / p1_2))
        else:  # otherwise, set the capacitance to zero
            model = 'w/o Parasitic C'
            chi2r = chi2r_2
            R_fb_i = p1_1[0]
            C_fb_i = 0
            if cov_x_1 is None:
                cov_x_1 = [0]
            CI.append((100 * np.sqrt(chi2r_1 * np.diag(cov_x_1)) /
                       p1_1).tolist() + [0])
        feedback_records.append([model, df_i.shape[0], R_fb_i, CI[i][0],
                                 C_fb_i, CI[i][1], F, (1e3 * np.sqrt(chi2r)),
                                 p_value])

    calibration_df = pd.DataFrame(feedback_records,
                                  columns=['Model', 'N', 'R_fb', 'R-CI %',
                                           'C_fb', 'C-CI %', 'F',
                                           'sqrt(Chi2r*sigma^2)', 'p-value'])
    return calibration_df


def apply_calibration(df, calibration_df, calibration):
    '''
    Apply calibration values from `fit_fb_calibration` result to `calibration`
    object.
    '''
    from dmf_control_board import FeedbackResults

    for i, (R_fb, C_fb) in calibration_df[['R_fb', 'C_fb']].iterrows():
        calibration.R_fb[i] = R_fb
        calibration.C_fb[i] = C_fb

    cleaned_df = df.dropna()
    grouped = cleaned_df.groupby(['frequency', 'test_channel', 'repeat_index'])

    for (f, channel, repeat_index), group in grouped:
        r = FeedbackResults(group.V_actuation.iloc[0], f, 5.0,
                            group.V_hv.values, group.hv_resistor.values,
                            group.V_fb.values, group.fb_resistor.values,
                            calibration)
        # Update the measured capacitance values based on the updated
        # calibration model.
        df.loc[group.index, 'C'] = r.capacitance()


def update_fb_calibration(proxy, calibration):
    print "Updating feedback calibration values...\n"
    proxy.connect()

    # write new calibration parameters to the control board
    for i in range(0, len(calibration.R_fb)):
        proxy.set_series_resistor_index(1, i)
        proxy.set_series_resistance(1, calibration.R_fb[i])
        proxy.set_series_capacitance(1, calibration.C_fb[i])
    # reconnect to update settings
    proxy.connect()