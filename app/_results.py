import streamlit as st
import numpy as np
import pandas as pd
import csv
from pathlib import Path

WEIGHTS_URL = "data/UV Spectral Weighting Curves.csv"


def print_standard_zones(room):
    """
    display results of special calc zones
    """

    st.subheader("Photobiological Safety", divider="grey")
    skin = room.calc_zones["SkinLimits"]
    eye = room.calc_zones["EyeLimits"]
    SHOW_SKIN = True if skin.values is not None else False
    SHOW_EYES = True if eye.values is not None else False
    if SHOW_SKIN and SHOW_EYES:

        hours_skin_uw, hours_eye_uw = get_unweighted_hours_to_tlv(room)
        SKIN_EXCEEDED = True if hours_skin_uw < 8 else False
        EYE_EXCEEDED = True if hours_eye_uw < 8 else False

        # print the max values
        cols = st.columns([3, 3, 1])
        skin = room.calc_zones["SkinLimits"]
        skin_max = round(skin.values.max(), 3)
        color = "red" if SKIN_EXCEEDED else "blue"
        skin_str = "**:" + color + "[" + str(skin_max) + "]** " + skin.units

        eye = room.calc_zones["EyeLimits"]
        eye_max = round(eye.values.max(), 3)
        color = "red" if EYE_EXCEEDED else "blue"
        eye_str = "**:" + color + "[" + str(eye_max) + "]** " + eye.units
        with cols[0]:
            st.write("**Max Skin Dose (8 Hours)**: ", skin_str)
        with cols[1]:
            st.write("**Max Eye Dose (8 Hours)**: ", eye_str)

        SHOW_PLOTS = cols[2].checkbox("Show plots", value=True)
        if SHOW_PLOTS:
            cols[0].pyplot(skin.plot_plane(), **{"transparent": "True"})
            cols[1].pyplot(eye.plot_plane(), **{"transparent": "True"})
        st.divider()
        st.markdown(
            """
        **__With Monochromatic Assumption:__** 
        These results assume that all lamps in the simulation are perfectly 
        monochromatic 222nm sources. They don't rely on any data besides an
        ies file. For most *filtered* KrCl lamps, but not all, the 
        monochromatic approximation is a reasonable assumption.
        """
        )

        safety_results(hours_skin_uw, hours_eye_uw, st.columns(2), room)
        st.divider()

        # Now show the weighted versions
        st.markdown(
            """
        **__With Spectral Weighting:__** 
        These results take into account the spectra of the lamps in the
        simulation. Because Threshold Limit Values (TLVs) are calculated by 
        summing over the *entire* spectrum, not just the peak wavelength, some
        lamps may have effective TLVs substantially below the monochromatic TLVs
        at 222nm.
        """
        )
        hours_skin_w, hours_eye_w = get_weighted_hours_to_tlv(room)
        safety_results(hours_skin_w, hours_eye_w, st.columns(2), room)

    st.subheader("Efficacy", divider="grey")
    fluence = room.calc_zones["WholeRoomFluence"]
    if fluence.values is not None:
        avg_fluence = round(fluence.values.mean(), 3)
        fluence_str = ":blue[" + str(avg_fluence) + "] μW/cm2"
    else:
        fluence_str = None
    st.write("Average fluence: ", fluence_str)

    if fluence.values is not None:
        df = get_disinfection_table(avg_fluence, room)
        st.dataframe(df, hide_index=True)

    st.subheader("Indoor Air Chemistry", divider="grey")
    if fluence.values is not None:
        ozone_ppb = calculate_ozone_increase(room)
        ozone_color = "red" if ozone_ppb > 5 else "blue"
        ozone_str = f":{ozone_color}[**{round(ozone_ppb,2)} ppb**]"
    else:
        ozone_str = "Not available"
    st.write(f"Air changes from ventilation: **{room.air_changes}**")
    st.write(f"Ozone decay constant: **{room.ozone_decay_constant}**")
    st.write(f"Estimated increase in indoor ozone: {ozone_str}")


def safety_results(hours_skin, hours_eye, cols, room):
    """print safety results by hours to TLV"""

    hours_to_tlv = min([hours_skin, hours_eye])
    if hours_to_tlv > 8:
        hour_str = ":blue[Indefinite]"
    else:
        hour_str = f":red[{round(hours_to_tlv,2)}]"
        dim = round((hours_to_tlv / 8) * 100, 1)
        hour_str += f" *(To be compliant with TLVs, this lamp must be dimmed to {dim}% of its present power)*"
    st.write(f"Hours before Threshold Limit Value is reached: {hour_str}")


def get_unweighted_hours_to_tlv(room):
    """
    calculate hours to tlv without taking into account lamp spectra
    """

    skin_standard, eye_standard = _get_standards(room.standard)
    mono_skinmax, mono_eyemax = _get_mono_limits(222, room)

    skin_limits = room.calc_zones["SkinLimits"]
    eye_limits = room.calc_zones["EyeLimits"]

    skin_hours = mono_skinmax * 8 / skin_limits.values.max()
    eye_hours = mono_eyemax * 8 / eye_limits.values.max()
    return skin_hours, eye_hours


def get_weighted_hours_to_tlv(room):
    """
    calculate the hours to tlv in a particular room, given a particular installation of lamps

    technically speaking; in the event of overlapping beams, it is possible to check which
    lamps are shining on that spot and what their spectra are. this function currently doesn't do that

    TODO: good lord this function is a nightmare. let's make it less horrible eventually
    """

    skin_standard, eye_standard = _get_standards(room.standard)
    mono_skinmax, mono_eyemax = _get_mono_limits(222, room)

    skin_limits = room.calc_zones["SkinLimits"]
    eye_limits = room.calc_zones["EyeLimits"]

    skin_hours, eyes_hours, skin_maxes, eye_maxes = _tlvs_over_lamps(room)

    # now check that overlapping beams in the calc zone aren't pushing you over the edge
    # max irradiance in the wholeplane
    global_skin_max = round(skin_limits.values.max() / 3.6 / 8, 3)  # to uW/cm2
    global_eye_max = round(eye_limits.values.max() / 3.6 / 8, 3)
    # max irradiance on the plane produced by each lamp
    local_skin_max = round(max(skin_maxes), 3)
    local_eye_max = round(max(eye_maxes), 3)

    if global_skin_max > local_skin_max or global_eye_max > local_eye_max:
        # first pick a lamp to use the spectra of. one with a spectra is preferred.
        chosen_lamp = _select_representative_lamp(room, skin_standard)
        if len(chosen_lamp.spectra) > 0:
            # calculate weighted if possible
            new_skin_hours = _get_weighted_hours(
                chosen_lamp, global_skin_max, skin_standard
            )
            new_eye_hours = _get_weighted_hours(
                chosen_lamp, global_eye_max, eye_standard
            )
        else:
            # these will be in mJ/cm2/8 hrs
            new_skin_hours = mono_skinmax * 8 / skin_limits.values.max()
            new_eye_hours = mono_eyemax * 8 / eye_limits.values.max()

        skin_hours.append(new_skin_hours)
        eyes_hours.append(new_eye_hours)
    return min(skin_hours), min(eyes_hours)


def _get_standards(standard):
    """return the relevant skin and eye limit standards"""
    if "ANSI IES RP 27.1-22" in standard:
        skin_standard = "ANSI IES RP 27.1-22 (Skin)"
        eye_standard = "ANSI IES RP 27.1-22 (Eye)"
    elif "IEC 62471-6:2022" in standard:
        skin_standard = "IEC 62471-6:2022 (Eye/Skin)"
        eye_standard = skin_standard
    else:
        raise KeyError(f"Room standard {standard} is not valid")

    return skin_standard, eye_standard


def _get_mono_limits(wavelength, room):
    """
    load the monochromatic skin and eye limits at a given wavelength
    """
    csv_data = open(WEIGHTS_URL, mode="r", newline="")
    reader = csv.reader(csv_data, delimiter=",")
    headers = next(reader, None)  # get headers

    # read standards
    data = {}
    for header in headers:
        data[header] = []
    for row in reader:
        for header, value in zip(headers, row):
            data[header].append(float(value))

    skin_standard, eye_standard = _get_standards(room.standard)
    skindata = dict(zip(data[headers[0]], data[skin_standard]))
    eyedata = dict(zip(data[headers[0]], data[eye_standard]))

    return 3 / skindata[wavelength], 3 / eyedata[wavelength]


def _tlvs_over_lamps(room):
    """calculate the hours to TLV over each lamp in the calc zone"""

    skin_standard, eye_standard = _get_standards(room.standard)
    mono_skinmax, mono_eyemax = _get_mono_limits(222, room)

    # iterate over all lamps
    hours_to_tlv_skin, hours_to_tlv_eye = [], []
    skin_maxes, eye_maxes = [], []
    for lamp_id, lamp in room.lamps.items():
        # get max irradiance shown by this lamp upon both zones
        skin_irradiance = lamp.max_irradiances["SkinLimits"]
        eye_irradiance = lamp.max_irradiances["EyeLimits"]

        if len(lamp.spectra) > 0:
            # if lamp has a spectra associated with it, calculate the weighted spectra
            skin_hours = _get_weighted_hours(lamp, skin_irradiance, skin_standard)
            eye_hours = _get_weighted_hours(lamp, eye_irradiance, eye_standard)
        else:
            # if it doesn't, first, yell.
            st.warning(
                f"{lamp.name} does not have an associated spectra. Photobiological safety calculations will be inaccurate."
            )
            # then just use the monochromatic approximation
            skin_hours = mono_skinmax * 8 / skin_irradiance
            eye_hours = mono_eyemax * 8 / eye_irradiance
        hours_to_tlv_skin.append(skin_hours)
        hours_to_tlv_eye.append(eye_hours)
        skin_maxes.append(skin_irradiance)
        eye_maxes.append(eye_irradiance)
    if len(room.lamps.items()) == 0:
        hours_to_tlv_skin, hours_to_tlv_eye = [np.inf], [np.inf]
        skin_maxes, eye_maxes = [0], [0]

    return hours_to_tlv_skin, hours_to_tlv_eye, skin_maxes, eye_maxes


def _get_weighted_hours(lamp, irradiance, standard):
    """
    calculate hours to tlv for a particular lamp, calc zone, and standard
    """

    # get spectral data for this lamp
    wavelength = lamp.spectra["Unweighted"][0]
    rel_intensities = lamp.spectra["Unweighted"][1]
    # determine total power in the spectra as it corresponds to total power
    idx = np.intersect1d(np.argwhere(wavelength >= 200), np.argwhere(wavelength <= 280))
    spectral_power = _sum_spectrum(wavelength[idx], rel_intensities[idx])
    ratio = irradiance / spectral_power
    power_distribution = rel_intensities[idx] * ratio  # true spectra at calc plane
    # load weights according to the standard
    weighting = lamp.spectral_weightings[standard][1]

    # weight the normalized spectra
    weighted_spectra = power_distribution * weighting[idx]
    # sum to get weighted power
    weighted_power = _sum_spectrum(wavelength[idx], weighted_spectra)

    seconds_to_tlv = 3000 / weighted_power
    hours_to_tlv = seconds_to_tlv / 3600
    return hours_to_tlv


def _sum_spectrum(wavelength, intensity):
    """
    sum across a spectrum
    """
    weighted_intensity = [
        intensity[i] * (wavelength[i] - wavelength[i - 1])
        for i in range(1, len(wavelength))
    ]
    return sum(weighted_intensity)


def _select_representative_lamp(room, standard):
    """
    select a lamp to use for calculating the spectral limits in the event
    that no single lamp is contributing exclusively to the TLVs
    """
    if len(set([lamp.filename for lamp_id, lamp in room.lamps.items()])) <= 1:
        # if they're all the same just use that one.
        lamp_id = list(room.lamps.keys())[0]
        chosen_lamp = room.lamps[lamp_id]
    else:
        # otherwise pick the least convenient one
        weighted_sums = {}
        for lamp_id, lamp in room.lamps.items():
            # iterate through all lamps and pick the one with the highest value sum
            if len(lamp.spectra) > 0:
                # either eye or skin standard can be used for this purpose
                weighted_sums[lamp_id] = lamp.spectra[standard].sum()

        if len(weighted_sums) > 0:
            chosen_id = max(weighted_sums, key=weighted_sums.get)
            chosen_lamp = room.lamps[chosen_id]
        else:
            # if no lamps have a spectra then it doesn't matter. pick any lamp.
            lamp_id = list(room.lamps.keys())[0]
            chosen_lamp = room.lamps[lamp_id]
    return chosen_lamp


def get_disinfection_table(fluence, room):

    """assumes all lamps are GUV222. in the future will need something cleverer than this"""

    wavelength = 222

    fname = Path("./data/disinfection_table.csv")
    df = pd.read_csv(fname)
    df = df[df["Medium"] == "Aerosol"]
    df = df[df["wavelength [nm]"] == wavelength]
    keys = ["Species", "Medium (specific)", "k [cm2/mJ]", "Ref", "Full Citation"]

    df = df[keys].fillna(" ")

    volume = room.get_volume()

    # convert to cubic feet for cfm
    if room.units == "meters":
        volume = volume / (0.3048 ** 3)

    df["eACH-UV"] = (df["k [cm2/mJ]"] * fluence * 3.6).round(2)
    df["CADR-UV [cfm]"] = (df["eACH-UV"] * volume / 60).round(2)
    df["CADR-UV [lps]"] = (df["CADR-UV [cfm]"] * 0.47195).round(2)
    df = df.rename(
        columns={"Medium (specific)": "Medium", "Full Citation": "Reference"}
    )

    newkeys = [
        "Species",
        # "Medium",
        "k [cm2/mJ]",
        "eACH-UV",
        "CADR-UV [cfm]",
        "CADR-UV [lps]",
        "Reference",
    ]
    df = df[newkeys]

    return df


def calculate_ozone_increase(room):
    """
    ozone generation constant is currently hardcoded to 10 for GUV222
    this should really be based on spectra instead
    but this is a relatively not very big deal, because
    """
    avg_fluence = room.calc_zones["WholeRoomFluence"].values.mean()
    ozone_gen = 10  # hardcoded for now, eventually should be based on spectra bu
    ach = room.air_changes
    ozone_decay = room.ozone_decay_constant
    ozone_increase = avg_fluence * ozone_gen / (ach + ozone_decay)
    return ozone_increase
