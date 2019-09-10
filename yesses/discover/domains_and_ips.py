import requests
import json
import logging
import dns.resolver
import dns.rdtypes.IN.A
import dns.rdtypes.IN.AAAA
from yesses.module import YModule, unwrap_key

log = logging.getLogger('discover/domains_and_ips')

class DomainsAndIPs(YModule):
    """Based on domain names as "seeds", tries to find new domain names by
guessing expansions for wildcards and expanding CNAMEs. Finds IP
addresses from A and AAAA records.

#### Examples ####
This example expands domains from a list of domain seeds and the TLS names found with `discover TLS Certificates`. The alerting assumes that a whitelist of IP addresses (`Good-IPs`) exists.
```
  - discover Domains and IPs:
      seeds: use Domain-Seeds and TLS-Names
      resolvers: use DNS-Resolvers
    find:
      - IPs
      - Domains
      - DNS-Entries
    expect:
      - no added IPs, otherwise alert high
      - no added Domains, otherwise alert high
      - no added DNS-Entries, otherwise alert high
      - all IPs in Good-IPs, otherwise alert high
```

In this example, the same module is used to check if homoglyph (or homograph) domains (similar-looking domain names) have been registered. This example assumes that a list of such domains has been generated before.

```
data:
  Homoglyph-Domains:
    - eхample.com  # note that "х" is a greek character, not the latin "x"
    - 3xample.com
      (...)

run:
    (...)
  - discover Domains and IPs:
      seeds: use Homoglyph-Domains
      resolvers: use DNS-Resolvers
    find:
      - Domains as Homoglyph-Matches
    expect:
      - no Homoglyph-Matches, otherwise alert high
```

    """
    
    INPUTS = [
        ('seeds', ['domain'], 'List of initial domains to start search from'),
        ('resolvers', ['ip'], 'List of DNS resolvers to use'),
    ]

    OUTPUTS = [
        ('Domains', ['domain'], 'List of domains found'),
        ('IPs', ['ip'], 'List of IPs found'),
        ('DNS-Entries', ['domain', 'ip'], 'Pairs of (domain, IP) associations'),
        ('Ignored-Domains', ['domain'], 'CNAME targets that are not a subdomain of one of the seeding domains; these are not expanded further and are not contained in the other results.'),
    ]
    
    base_url = "https://crt.sh/?q=%25.{}&output=json"
    user_agent = 'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:40.0) Gecko/20100101 Firefox/40.1'
    rdtypes = [1, 28] # A and AAAA

    @unwrap_key('seeds', 'domain')
    @unwrap_key('resolvers', 'ip')
    def __init__(self, step, seeds, resolvers=None):
        self.step = step
        self.seeds = seeds
        log.info(f'Using seeds: {seeds!r}')
        self.resolver = dns.resolver.Resolver()
        if resolvers is not None:
            self.resolver.nameservers = resolvers

    def run(self):
        self.domains = set(self.seeds)
        self.ignored_domains = set()
        for d in self.seeds:
            self.domains |= self.domains_from_ctlog(d)
        log.info(f'Domains before expanding: {self.domains}')
        self.expand_from_cnames()
        log.info(f'Found {len(self.domains)} domains after expanding CNAMEs')
        self.expand_wildcards()
        log.info(f'Found {len(self.domains)} domains after expanding wildcards')
        self.ips_from_domains()
        log.info(f'Left with {len(self.domains)} domains after checking for records')
        

    def domains_from_ctlog(self, query_domain):
        url = self.base_url.format(query_domain)
        req = requests.get(url, headers={'User-Agent': self.user_agent})

        if not req.ok:
            raise Exception(f"Cannot retrieve certificate transparency log from {url}")
        content = req.content.decode('utf-8')
        data = json.loads(content)
        return set(crt['name_value'] for crt in data)

    def expand_from_cnames(self):
        newdomains = set()
        
        for d in self.domains:
            if d.startswith('*'):
                continue
            try:
                answers = self.resolver.query(d, 'CNAME')
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
                continue
            
            for rdata in answers:
                candidate = rdata.target.to_text()[:-1]
                for s in self.seeds:
                    if candidate.endswith(f'.{s}'):
                        newdomains.add(candidate)
                        break
                else:
                    self.ignored_domains |= set(candidate)
                        
        self.domains |= newdomains
        self.results['Ignored-Domains'] = [{'domain': d} for d in self.ignored_domains]

    def expand_wildcards(self):
        subdomains = set()
        for d in self.domains:
            if d.startswith('*'):
                continue
            for s in self.seeds:
                if d.endswith('.' + s):
                    subdomains.add(d[:-(len(s)+1)])
        log.info(f'Found subdomains: {subdomains!r}')

        newdomainset = set()
        for d in self.domains:
            if not d.startswith('*'):
                newdomainset.add(d)
            else:
                for subdomain in subdomains:
                    newdomain = f'{subdomain}{d[1:]}'
                    newdomainset.add(newdomain)
                    
        self.domains = newdomainset

    def ips_from_domains(self):
        newdomainset = set()
        ips = []
        domains_to_ips = []

        for d in self.domains:
            for rdtype in self.rdtypes:
                log.debug(f"Checking DNS: {rdtype} {d}")
                try:
                    answers = self.resolver.query(d, rdtype)
                except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.resolver.NoAnswer) as e:
                    log.debug(f"Not found: {e}")
                else:
                    for answer in answers:
                        domains_to_ips.append({'domain': d, 'ip': answer.address})
                        ips.append(answer.address)
                    newdomainset.add(d)

        self.results['Domains'] = [{'domain': d} for d in newdomainset]
        self.results['DNS-Entries'] = domains_to_ips
        self.results['IPs'] = [{'ip': i} for i in set(ips)]
        

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    import sys
    d = DiscoverDomainsAndIPs(sys.argv[1:])
    print (d.run())
