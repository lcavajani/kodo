#!/usr/bin/env python3

import argparse
import os
import yaml
import json
import logging
import subprocess
import sys


def parse_args():
    """ Parse command line arguments """
    parser = argparse.ArgumentParser(description="Manage team resources")

    subparsers = parser.add_subparsers(help="Choose a resource", dest="rsc")

    parser_kobay = subparsers.add_parser("kobay", help="Manage Kobay")
    parser_kobay.add_argument("action", nargs="?", choices=["customize", "deploy"])
    parser_kobay.add_argument("--reckoner-course-file", nargs="?", required=False,
                              default=os.environ.get("KOBAY_RECKONER_COURSE_FILE", "kobay.yaml"),
                              action="store", help="Kobay reckoner course file path")
    parser_kobay.add_argument("--kubeconfig", nargs="?", required=False,
                              default=os.environ.get("KOBAY_KUBECONFIG", None),
                              action="store", help="Kobay kubeconfig path")
    parser_kobay.add_argument("--doktor-domain", nargs="?", required=False,
                              default=os.environ.get("DOKTOR_DOMAIN", None),
                              action="store", help="Must be a wildcard DNS pointing to a worker on the Doktor cluster where the services are exposed")

    parser_doktor = subparsers.add_parser("doktor", help="Manage Doktor")
    parser_doktor.add_argument("action", nargs="?", choices=["customize", "deploy"])
    parser_doktor.add_argument("--reckoner-course-file", nargs="?", required=False,
                               default=os.environ.get("KOBAY_RECKONER_COURSE_FILE", "doktor.yaml"),
                               action="store", help="Doktor reckoner course file path")
    parser_doktor.add_argument("--kubeconfig", nargs="?", required=False,
                               default=os.environ.get("DOKTOR_KUBECONFIG", None),
                               action="store", help="Doktor kubeconfig path")
    parser_doktor.add_argument("--doktor-domain", nargs="?", required=False,
                               default=os.environ.get("DOKTOR_DOMAIN", None),
                               action="store", help="Must be a wildcard DNS pointing to a worker on the Doktor cluster where the services are exposed")
    parser_doktor.add_argument("--kobay-tfstate", nargs="?", required=False,
                               default=os.environ.get("KOBAY_TFSTATE", "../terraform.tfstate"),
                               action="store", help="Path to the state file of the Kobay cluster to read")
    parser_doktor.add_argument("--cap-test-app-url", nargs="?", required=False,
                               default=os.environ.get("CAP_TEST_APP_URL", None),
                               action="store", help="URL of the test app deployed on CAP")

    args = parser.parse_args()

    if not args.rsc or not args.action:
        parser.print_help()
        sys.exit(1)

    return args


def load_yaml(yaml_file):
    """ Load file and read yaml """
    try:
        with open(yaml_file, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
        return(content)
    except IOError as e:
        logging.exception("I/O error: {0}".format(e))
    except yaml.YAMLError as ey:
        logging.exception("Error in yaml file: {0}".format(ey))


def open_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except IOError as e:
        logging.exception("I/O error: {0}".format(e))


def write_file(filepath, content, mode="w"):
    """ Write string or binary to file """
    try:
        with open(filepath, mode) as f:
            f.write(content)
    except IOError as e:
        logging.error("I/O error: {0}".format(e))


def read_tf_output(tfstate_path):
    tf = open_file(tfstate_path)
    tf = json.loads(tf)
    return tf["modules"][0]["outputs"]


def run_reckoner(course_file, kubeconfig, target, **kwargs):
    script_path = os.path.dirname(os.path.realpath(__file__))
    env_vars = os.environ.copy()
    return subprocess.run("helm init --client-only && reckoner --log-level DEBUG plot {0}".format(course_file),
                          env={**env_vars,
                               **{"HELM_HOME": os.path.join(script_path,
                                                            (".helm_" + target)),
                                  "KUBECONFIG": kubeconfig}},
                          shell=True, **kwargs)


def get_helm_values_path(course, component):
    return course["charts"][component]["files"][0]


def update_ingress_host(course, component, doktor_domain):
    component_values_path = get_helm_values_path(course, component)
    component_values = load_yaml(component_values_path)
    component_values["ingress"]["hosts"][0] = "{0}.{1}".format(component, doktor_domain)
    write_file(component_values_path, yaml.dump(component_values, default_flow_style=False))


def main():
    logging.basicConfig(level=logging.INFO)
    global kubeconfig

    args = parse_args()
    rsc = args.rsc
    action = args.action
    reckoner_course_file = args.reckoner_course_file
    course = load_yaml(args.reckoner_course_file)
    doktor_domain = args.doktor_domain

    # TODO: add checks for env vars
    if rsc == "kobay":
        if action == "customize":
            logging.info("Customizing kobay helm charts")

            # Customize FLUENTD-ELASTICSEARCH
            f_elasticsearch_values_path = get_helm_values_path(course, "fluentd-elasticsearch")
            f_elasticsearch_values = load_yaml(f_elasticsearch_values_path)
            f_elasticsearch_values["elasticsearch"]["host"] = "{0}.{1}".format(component, doktor_domain)
            write_file(f_elasticsearch_values_path, yaml.dump(f_elasticsearch_values, default_flow_style=False))

        if action == "deploy":
            run_reckoner(reckoner_course_file, args.kubeconfig, "kobay")

    if rsc == "doktor":
        if action == "customize":
            logging.info("Customizing doktor helm charts")

            # Read tfstate so we can get ip of the nodes
            tfstate = read_tf_output(args.kobay_tfstate)

            prometheus_values_path = get_helm_values_path(course, "prometheus")
            prometheus_values = load_yaml(prometheus_values_path)
            prometheus_values["server"]["ingress"]["hosts"][0] = "{0}.{1}".format("prometheus-server", doktor_domain)

            # Create a new yaml so we can edit the string configuration declared with `extraScrapeConfigs: |`
            extra_scrape_config = yaml.safe_load(prometheus_values["extraScrapeConfigs"])

            for index, conf in enumerate(extra_scrape_config):
                job_name = conf["job_name"]
                # Add masters and workers nodes to be scraped
                if (job_name == "kobay_prometheus-node-exporter" or job_name == "kobay_kube-state-metrics"):
                    extra_scrape_config[index]["static_configs"][0]["targets"] = (tfstate["ip_masters"]["value"] + tfstate["ip_workers"]["value"])

                # Add CAP test-app URL
                if args.cap_test_app_url:
                    if job_name == "cap_test-app":
                        extra_scrape_config[index]["static_configs"][0]["targets"] = [args.cap_test_app_url]

            # Update conf in original conf
            prometheus_values["extraScrapeConfigs"] = yaml.dump(extra_scrape_config, default_flow_style=False)
            write_file(prometheus_values_path, yaml.dump(prometheus_values))

            # Configure ingress hosts
            for c in ["elasticsearch-master", "grafana", "kibana", "prometheus-blackbox-exporter"]:
                update_ingress_host(course, c, doktor_domain)


        if action == "deploy":
            run_reckoner(reckoner_course_file, args.kubeconfig, "doktor")


if __name__ == "__main__":
    main()
